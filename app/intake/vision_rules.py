"""Photo-only extraction rules. No fault diagnosis, model lookup or API calls."""

import math
import re

PHOTO_TYPES = {"BODY", "NAMEPLATE", "CONTROLLER", "NOT_BOILER", "UNKNOWN"}
MAKERS = {
    "KD_NAVIEN": ("경동나비엔", "경동", "나비엔", "KD NAVIEN", "NAVIEN"),
    "KITURAMI": ("귀뚜라미", "KITURAMI"),
    "RINNAI": ("린나이", "RINNAI"),
    "DAESUNG": ("대성쎌틱", "대성", "쎌틱", "DAESUNG", "CELTIC"),
    "LOTTE": ("롯데기공", "롯데", "LOTTE"),
}
FIELDS = (
    "manufacturer",
    "manufacturer_other",
    "manufacturer_evidence",
    "model",
    "series",
    "controller_model",
    "display_text",
    "error_code",
)
KEYS = {"photo_type", "visible_text", "note", *FIELDS}
OBJECTS = {
    "BOILER",
    "BOILER_CONTROLLER",
    "GAS_METER",
    "WATER_METER",
    "AC_REMOTE",
    "OTHER",
    "UNKNOWN",
}
KINDS = {"body_logo", "nameplate", "controller_logo", "controller_label", "display", "series"}
EVIDENCE = {
    "body_logo": "BODY_LOGO",
    "nameplate": "NAMEPLATE",
    "controller_logo": "CONTROLLER_LOGO",
}
MODEL_LABEL = re.compile(
    r"(?:모델\s*명(?:\s*및\s*기[종증])?|MODEL(?:\s*NO\.?)?|형식)\s*[:：]?\s*", re.I
)
SERIAL_LABEL = re.compile(r"(?:제조\s*번호|일련\s*번호|S\s*/\s*N|SERIAL|LOT)\s*[:：]?", re.I)
ERROR = re.compile(
    r"(?<![A-Z0-9.])(?:ER(?:\s*[-:]?\s*\d{1,3})?|[A-Z]{1,3}\d{1,3}|[EF]\s*[-:]?\s*\d{1,3})(?![A-Z0-9.])",
    re.I,
)
TEMP = re.compile(r"(?:°\s*[CF]|℃|℉|도씨)", re.I)
ERROR_MARK = re.compile(r"(?:에러|점검|ERROR)", re.I)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def probability(value):
    return type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1


def bbox_valid(box):
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(probability(x) for x in box)
        and box[0] < box[2]
        and box[1] < box[3]
    )


def validate_result(result):
    require(isinstance(result, dict) and set(result) == KEYS, "result: exactly 11 fields required")
    require(result["photo_type"] in PHOTO_TYPES, "invalid photo_type")
    require(
        isinstance(result["visible_text"], list)
        and all(isinstance(x, str) and x for x in result["visible_text"]),
        "invalid visible_text",
    )
    for field in (*FIELDS, "note"):
        require(
            result[field] is None or isinstance(result[field], str) and bool(result[field].strip()),
            "invalid " + field,
        )
    require(
        result["manufacturer"] is None or result["manufacturer"] in {*MAKERS, "OTHER"},
        "invalid manufacturer",
    )
    require(
        result["manufacturer_evidence"] is None
        or result["manufacturer_evidence"] in set(EVIDENCE.values()),
        "invalid manufacturer_evidence",
    )
    require(
        (result["manufacturer"] is None) == (result["manufacturer_evidence"] is None),
        "manufacturer/evidence mismatch",
    )
    require(
        (result["manufacturer"] == "OTHER") == (result["manufacturer_other"] is not None),
        "OTHER/name mismatch",
    )
    if result["photo_type"] in {"NOT_BOILER", "UNKNOWN"}:
        require(
            not result["visible_text"] and all(result[x] is None for x in FIELDS),
            "rejected photo has extracted data",
        )


def validate_observation(obs):
    require(isinstance(obs, dict), "observation must be object")
    require(
        obs.get("object_type") in OBJECTS and obs.get("photo_type") in PHOTO_TYPES,
        "invalid object/photo type",
    )
    require(isinstance(obs.get("regions"), list), "regions must be list")
    seen = set()
    for region in obs["regions"]:
        require(isinstance(region, dict), "invalid region")
        require(
            isinstance(region.get("id"), str) and region["id"] and region["id"] not in seen,
            "region id must be unique",
        )
        seen.add(region["id"])
        require(
            region.get("kind") in KINDS and bbox_valid(region.get("bbox")),
            "invalid region kind/bbox",
        )
        require(isinstance(region.get("texts"), list), "region texts must be list")
        for token in region["texts"]:
            require(
                isinstance(token, dict)
                and isinstance(token.get("text"), str)
                and token["text"].strip(),
                "invalid OCR text",
            )
            require(
                probability(token.get("score")) and type(token.get("complete")) is bool,
                "invalid OCR score/completeness",
            )
            require(bbox_valid(token.get("bbox")), "invalid OCR bbox")
            a, b, c, d = token["bbox"]
            x, y, z, w = region["bbox"]
            require(
                a >= x - 0.015 and b >= y - 0.015 and c <= z + 0.015 and d <= w + 0.015,
                "OCR evidence outside its region",
            )


def known_makers(text):
    hits = set()
    for maker, aliases in MAKERS.items():
        for alias in aliases:
            # Do not treat NAVIEN inside an unrelated model token as a logo.
            pattern = r"(?<![A-Z0-9가-힣])" + re.escape(alias) + r"(?![A-Z0-9가-힣])"
            if re.search(pattern, text.upper()):
                hits.add(maker)
    return hits


def label_value(token, tokens):
    """Complete same-line value, or a spatially adjacent same-row OCR box."""
    text = token["text"]
    label = MODEL_LABEL.search(text)
    if not label or SERIAL_LABEL.search(text[: label.start()]):
        return None
    tail = SERIAL_LABEL.split(text[label.end() :])[0].strip()
    if tail:
        return tail
    x1, y1, x2, y2 = token["bbox"]
    nearby = []
    for other in tokens:
        if (
            other is token
            or MODEL_LABEL.search(other["text"])
            or SERIAL_LABEL.search(other["text"])
        ):
            continue
        a, b, c, d = other["bbox"]
        overlap = min(y2, d) - max(y1, b)
        if a >= x2 - 0.01 and a - x2 <= 0.25 and overlap >= 0.5 * min(y2 - y1, d - b):
            nearby.append(other["text"].strip())
    return nearby[0] if len(nearby) == 1 else None


def extract(obs, min_score=0.85):
    validate_observation(obs)
    require(probability(min_score), "invalid min_score")
    result = {
        "photo_type": obs["photo_type"],
        "visible_text": [],
        **dict.fromkeys(FIELDS),
        "note": None,
    }
    warnings, evidence = [], {}
    obj = obs["object_type"]
    if obj in {"GAS_METER", "WATER_METER", "AC_REMOTE", "OTHER"}:
        result["photo_type"] = "NOT_BOILER"
        return result, {"warnings": ["NON_BOILER_OBJECT"], "evidence": {}}
    if obj == "UNKNOWN" or obs["photo_type"] in {"UNKNOWN", "NOT_BOILER"}:
        result["photo_type"] = "UNKNOWN"
        result["note"] = "탐지 결과만으로 사진 종류를 확정하지 못했습니다."
        return result, {"warnings": ["OBJECT_NOT_CONFIRMED"], "evidence": {}}
    if (obj == "BOILER_CONTROLLER") != (obs["photo_type"] == "CONTROLLER"):
        result["photo_type"] = "UNKNOWN"
        result["note"] = "탐지 대상과 사진 종류가 일치하지 않습니다."
        return result, {"warnings": ["OBJECT_PHOTO_MISMATCH"], "evidence": {}}

    usable = []
    for region in obs["regions"]:
        tokens = []
        for token in region["texts"]:
            # Scores are reader scores, NOT LLM self-reported confidence.
            if token["score"] < min_score or not token["complete"]:
                warnings.append("LOW_SCORE_OR_CUT_TEXT:" + region["id"])
                continue
            tokens.append(token)
            if token["text"] not in result["visible_text"]:
                result["visible_text"].append(token["text"])
        usable.append((region, tokens))

    candidates = {x: [] for x in FIELDS}
    maker_candidates = []
    for region, tokens in usable:
        kind = region["kind"]
        texts = [t["text"] for t in tokens]
        if kind in EVIDENCE and not (obj == "BOILER_CONTROLLER" and kind != "controller_logo"):
            for maker in known_makers(" ".join(texts)):
                maker_candidates.append((maker, EVIDENCE[kind], region["id"]))
        if kind in {"nameplate", "controller_label"}:
            if (
                kind == "nameplate"
                and obj != "BOILER"
                or kind == "controller_label"
                and obj != "BOILER_CONTROLLER"
            ):
                continue
            field = "model" if kind == "nameplate" and obj == "BOILER" else "controller_model"
            for token in tokens:
                value = label_value(token, tokens)
                if (
                    value
                    and re.fullmatch(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣+(). /_-]*", value)
                    and not MODEL_LABEL.search(value)
                ):
                    candidates[field].append((value, region["id"]))
            # A dedicated controller-model crop may contain only the identifier.
            if (
                kind == "controller_label"
                and len(texts) == 1
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*[- ][A-Za-z0-9-]+", texts[0])
            ):
                candidates["controller_model"].append((texts[0], region["id"]))
        elif kind == "series" and obj == "BOILER":
            for text in texts:
                if not known_makers(text) and re.fullmatch(r"[A-Za-z][A-Za-z0-9 +_-]*", text):
                    candidates["series"].append((text, region["id"]))
        elif kind == "display" and texts:
            display = " ".join(texts)
            candidates["display_text"].append((display, region["id"]))
            codes = [
                m.group(0)
                for m in ERROR.finditer(display)
                if not re.match(
                    r"\s*(?:°\s*[CF]|℃|℉|도씨|V\b|KW\b|KPA\b)", display[m.end() :], re.I
                )
            ]
            if len(codes) == 1:
                candidates["error_code"].append((codes[0], region["id"]))
            elif ERROR_MARK.search(display) and not TEMP.search(display):
                numbers = re.findall(r"(?<![A-Z0-9])\d{1,3}(?![A-Z0-9])", display, re.I)
                if len(numbers) == 1:
                    candidates["error_code"].append((numbers[0], region["id"]))
            elif re.search(r"\d", display) and not TEMP.search(display):
                warnings.append("NUMERIC_DISPLAY_NEEDS_CONTEXT")

    makers = {x[0] for x in maker_candidates}
    if len(makers) == 1:
        # Prefer nameplate evidence; do not change the text itself.
        rank = {"NAMEPLATE": 0, "BODY_LOGO": 1, "CONTROLLER_LOGO": 2}
        maker, source, region_id = min(maker_candidates, key=lambda x: rank[x[1]])
        result["manufacturer"], result["manufacturer_evidence"] = maker, source
        evidence["manufacturer"] = region_id
    elif len(makers) > 1:
        warnings.append("CONFLICTING_MANUFACTURERS")
    else:
        warnings.append("MANUFACTURER_NOT_CONFIRMED")
    # OTHER needs a verified boiler-brand label; never infer it from an unknown company string.
    for field in ("model", "series", "controller_model", "display_text", "error_code"):
        values = {x[0] for x in candidates[field]}
        if len(values) == 1:
            result[field] = candidates[field][0][0]
            evidence[field] = candidates[field][0][1]
        elif len(values) > 1:
            warnings.append("CONFLICTING_VALUES:" + field)
    if result["error_code"] and (
        not result["display_text"] or result["error_code"] not in result["display_text"]
    ):
        result["error_code"] = None
        evidence.pop("error_code", None)
        warnings.append("DISPLAY_CONFLICT")
    if result["series"] and result["model"] and result["series"] in result["model"]:
        result["series"] = None
        evidence.pop("series", None)
    result["note"] = "확인되지 않은 항목은 비워 두었습니다." if warnings else None
    validate_result(result)
    return result, {"warnings": sorted(set(warnings)), "evidence": evidence}


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation")
    args = parser.parse_args()
    observed = json.loads(Path(args.observation).read_text(encoding="utf-8-sig"))
    answer, audit = extract(observed)
    print(json.dumps({"result": answer, "audit": audit}, ensure_ascii=False, indent=2))
