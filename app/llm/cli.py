"""Local sample calls and safe usage summaries.
로컬 샘플 호출·사용량 조회 CLI
"""

import argparse
import json
import os
from pathlib import Path

from app.core.config import Settings
from app.llm.gemini_provider import LOG_PATH, analyze_gemini
from app.llm.openai_provider import call_openai_from_environment, summarize_calls
from app.llm.usage_audit import demo_errors, summarize_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("gemini", "openai"), default="gemini")
    parser.add_argument("--mode", choices=("call", "usage", "errors"), default="call")
    parser.add_argument("--message")
    parser.add_argument("--image", type=Path)
    parser.add_argument(
        "--show-result", action="store_true", help="Print structured extraction to the terminal"
    )
    args = parser.parse_args()
    if args.mode == "usage":
        result = summarize_csv(LOG_PATH) if args.provider == "gemini" else summarize_calls()
    elif args.mode == "errors":
        result = demo_errors()
    else:
        if not args.message:
            parser.error("call mode requires --message.")
        # Only the local CLI loads .env; server entrypoints use process environment.
        settings = Settings()
        name = f"{args.provider.upper()}_API_KEY"
        secret = getattr(settings, name.lower())
        if secret is not None:
            os.environ[name] = secret.get_secret_value()
        try:
            if args.provider == "gemini":
                response = analyze_gemini(args.message, args.image)
                result = {
                    key: response[key] for key in ("request_id", "provider", "model", "usage")
                }
                if args.show_result:
                    result = response
            else:
                extraction, usage = call_openai_from_environment(
                    os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), args.message, args.image
                )
                result = {"provider": "openai", "usage": usage}
                if args.show_result:
                    result["extraction"] = extraction.model_dump(mode="json")
        except Exception as exc:
            # Do not print provider error messages that may contain private input.
            parser.exit(1, f"Call failed: {type(exc).__name__}. Check logs with --mode usage.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
