"""Grounded text/layout fallback for unreliable zero-shot object detections.

No filenames, labels, product database, or LLM are inputs to these rules.
"""

import re

from app.intake.vision_rules import ERROR, MODEL_LABEL, extract, known_makers, probability, require

NEGATIVE = re.compile(
    r"가스\s*계량기|수도\s*계량기|GAS\s+METER|WATER\s+METER|에어컨|AIR\s+CONDITIONER", re.I
)
THERMOSTAT = re.compile(
    r"THERMOSTAT|ROOM\s+CONTROLLER|실내\s*(?:온도)?\s*조절기|실내온도조절", re.I
)
BOILER_WORDS = re.compile(r"보일러|BOILER|CONDENSING|콘덴싱|연통", re.I)
PLATE_WORDS = (
    "모델명",
    "형식",
    "제조번호",
    "사용가스",
    "급배기",
    "소비전력",
    "정격",
    "제조원",
    "MODEL",
    "SERIAL",
)
SPEC_UNITS = (r"KCAL", r"KPA", r"\bKW", r"LNG", r"전압|주파수|정격")
CONTROLS = ("외출", "예약", "실내", "난방", "온수", "전원", "선택", "목욕", "온돌")
IDENTIFIER = re.compile(r"[A-Z]{1,6}-[A-Z0-9][A-Z0-9-]{1,18}", re.I)


def area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def bounds(boxes):
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def overlap(token, box):
    a = token["bbox"]
    intersection = max(0, min(a[2], box[2]) - max(a[0], box[0])) * max(
        0, min(a[3], box[3]) - max(a[1], box[1])
    )
    return intersection >= 0.6 * area(a)


def model_label_tokens(tokens):
    # Only explicit label rows, not arbitrary product or error codes on the case.
    rows = []
    for token in tokens:
        if MODEL_LABEL.search(token["text"]):
            rows.append(token)
            a = token["bbox"]
            rows.extend(
                t
                for t in tokens
                if t is not token
                and t["bbox"][0] >= a[2] - 0.01
                and min(a[3], t["bbox"][3]) - max(a[1], t["bbox"][1])
                >= 0.5 * min(a[3] - a[1], t["bbox"][3] - t["bbox"][1])
            )
    return list({id(t): t for t in rows}.values())


def visual_regions(image):
    """Classical rectangular-panel proposals, not YOLO predictions or probabilities."""
    import cv2
    import numpy as np

    im = image.copy()
    im.thumbnail((1600, 1600))
    gray = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    result = []
    for dark in (True, False):
        _, binary = cv2.threshold(
            gray, 0, 255, (cv2.THRESH_BINARY_INV if dark else cv2.THRESH_BINARY) | cv2.THRESH_OTSU
        )
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            polygon = cv2.approxPolyDP(contour, 0.025 * cv2.arcLength(contour, True), True)
            if not (
                len(polygon) == 4 if dark else 4 <= len(polygon) <= 8
            ) or not cv2.isContourConvex(polygon):
                continue
            x, y, bw, bh = cv2.boundingRect(contour)
            fraction = bw * bh / (w * h)
            rectangularity = cv2.contourArea(contour) / (bw * bh)
            if rectangularity < 0.75:
                continue
            mean = float(gray[y : y + bh, x : x + bw].mean())
            if (
                dark
                and 0.04 <= fraction <= 0.35
                and 1.1 <= bw / bh <= 4
                and mean < 145
                and x > 1
                and y > 1
                and x + bw < w - 1
                and y + bh < h - 1
            ):
                kind = "display"
            elif not dark and 0.25 <= fraction <= 0.9 and 1.05 <= bh / bw <= 3 and mean > 155:
                kind = "chassis"
            else:
                continue
            result.append(
                {
                    "class": kind,
                    "bbox": [x / w, y / h, (x + bw) / w, (y + bh) / h],
                    "source": "classical rectangular-panel rule",
                    "rectangularity": rectangularity,
                }
            )
    return result


def classify(detections, tokens, visual=()):
    text = " ".join(t["text"] for t in tokens)
    compact = re.sub(r"\s+", "", text).upper()
    if NEGATIVE.search(text):
        return "OTHER", "NOT_BOILER", "explicit non-boiler wording"
    plate = [
        t
        for t in tokens
        if any(w in re.sub(r"\s+", "", t["text"]).upper() for w in PLATE_WORDS)
        or any(re.search(p, t["text"], re.I) for p in SPEC_UNITS)
    ]
    plate_words = sum(w in compact for w in PLATE_WORDS)
    unit_groups = sum(bool(re.search(p, text, re.I)) for p in SPEC_UNITS)
    if THERMOSTAT.search(text):
        return "BOILER_CONTROLLER", "CONTROLLER", "explicit room-controller wording"
    plate_box = bounds([t["bbox"] for t in plate]) if plate else None
    text_box = bounds([t["bbox"] for t in tokens]) if tokens else None
    table_layout = (
        plate_box and text_box and plate_box[3] - plate_box[1] >= 0.35 and area(text_box) >= 0.35
    )
    if (
        (plate_words >= 3 or plate_words >= 2 and unit_groups >= 3)
        and table_layout
        and (known_makers(text) or BOILER_WORDS.search(text))
    ):
        return "BOILER", "NAMEPLATE", "multiple specification labels across the photo"
    control_count = sum(w in compact for w in CONTROLS)
    screens = [d for d in detections if d["class"] == "display" and area(d["bbox"]) >= 0.07]
    visual_screens = [d for d in visual if d["class"] == "display" and area(d["bbox"]) >= 0.07]
    # ponytail: printed labels only classify text-rich photos; retest with labeled images.
    if plate_words < 3 and (control_count >= 4 or control_count >= 3 and screens):
        return (
            "BOILER_CONTROLLER",
            "CONTROLLER",
            "multiple heating-control labels, with display support when sparse",
        )
    if known_makers(text) and "연통" in compact:
        return "BOILER", "BODY", "printed flue warning and manufacturer"
    if plate_words < 3 and visual_screens and (control_count >= 2 or known_makers(text)):
        return (
            "BOILER_CONTROLLER",
            "CONTROLLER",
            "large rectangular display with heating labels or printed manufacturer",
        )
    if known_makers(text) and BOILER_WORDS.search(text):
        return "BOILER", "BODY", "boiler wording and visible manufacturer"
    bodies = [
        d
        for d in detections
        if d["class"] == "boiler_body" and d["score"] >= 0.7 and area(d["bbox"]) >= 0.25
    ]
    if bodies and known_makers(text) and not control_count:
        return "BOILER", "BODY", "detected boiler body and printed manufacturer"
    if known_makers(text) and any(d["class"] == "chassis" for d in visual):
        return "BOILER", "BODY", "vertical equipment panel and printed manufacturer (heuristic)"
    return "UNKNOWN", "UNKNOWN", "insufficient consistent photo evidence"


def extract_with_text(detections, tokens, visual=(), min_score=0.85):
    require(probability(min_score), "invalid OCR threshold")
    usable = [t for t in tokens if t["score"] >= min_score and t["complete"]]
    obj, photo, reason = classify(detections, usable, visual)
    regions = []

    def add(kind, texts, source, box=None):
        if texts:
            regions.append(
                {
                    "id": f"fallback_{len(regions)}",
                    "kind": kind,
                    "bbox": box or bounds([t["bbox"] for t in texts]),
                    "texts": texts,
                    "source": source,
                }
            )

    if photo in {"BODY", "NAMEPLATE", "CONTROLLER"}:
        if photo == "NAMEPLATE":
            add("nameplate", usable, "text/layout rules", [0, 0, 1, 1])
        else:
            for token in usable:
                if known_makers(token["text"]):
                    add(
                        "controller_logo" if photo == "CONTROLLER" else "body_logo",
                        [token],
                        "recognized printed brand",
                    )
            if photo == "BODY":
                add("nameplate", model_label_tokens(usable), "explicit model-label rows")
            else:
                add(
                    "controller_label",
                    model_label_tokens(usable),
                    "explicit controller model-label rows",
                )
                for token in usable:
                    if IDENTIFIER.fullmatch(token["text"].strip()) and not ERROR.fullmatch(
                        token["text"].strip()
                    ):
                        add("controller_label", [token], "isolated controller identifier")
        for detection in [] if photo == "NAMEPLATE" else [*detections, *visual]:
            if detection["class"] == "display":
                found = [t for t in usable if overlap(t, detection["bbox"])]
                if found:
                    add(
                        "display",
                        found,
                        detection.get("source", "YOLO") + " display + OCR overlap",
                        bounds([detection["bbox"], *[t["bbox"] for t in found]]),
                    )
    obs = {"object_type": obj, "photo_type": photo, "regions": regions}
    return extract(obs, min_score=min_score)[0]
