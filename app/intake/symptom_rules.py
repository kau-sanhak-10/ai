"""진단 없이, 고객이 구체적인 상태를 설명했는지만 판단한다."""

import re

# 대상 이름만 언급한 문의와 관찰한 상태를 구별한다. 임의의 단어 사이를
# 무제한 연결하지 않고 조사·상태 수식어 뒤에 실제 상태가 오는 경우만 인정한다.
_TARGET_STATE = re.compile(
    r"(?:온수|따뜻한물|뜨거운물|더운물|난방|방바닥|바닥|전원|점화|차단기|압력)"
    r"(?:이|가|은|는|도|만|쪽|상태|에서|의)*"
    r"(?:전혀|계속|자꾸|갑자기|아예|잘|가끔|너무|거의|완전히|제대로|정상적으로)*"
    r"(?:안(?:나[오와옴옵]|돼|되|됩|됨|켜|들어|붙|돌|올라|내려)|"
    r"(?:나오|켜지|들어오|작동하|점화되|올라가|내려가)지않|"
    r"불량|불규칙|먹통|끊|차갑|차가워|미지근|뜨겁|약해|약함|낮아|낮음|높아|높음|떨어|멈|꺼)"
)
_OBSERVABLE = re.compile(
    r"(?:누수|물샘|누전|동파|과열|가스냄새|타는냄새|탄내)$|"
    r"(?:누수|소음|진동|냄새|연기|불꽃)(?:이|가|는|은|도|만)?"
    r"(?:나|남|있|심|발생|보)|"
    r"물(?:이|가)?새(?:요|어|는|고)|"
    r"보일러(?:가|는|도)?(?:안켜|켜지지않)|"
    r"(?:차가운|미지근한)물(?:이|만|도|가)?나|"
    r"(?:에러|오류)(?:코드)?(?:가|이|는)?(?:[a-z]?\d+|표시|떠|발생)"
)

# ponytail: only explicit current reports bypass the model; ambiguous phrasing uses Bedrock.
GAS_LEAK_REASON = "POSSIBLE_GAS_LEAK"
_GAS_REPORT = re.compile(
    r"(?:(?:보일러(?:가?있는곳|주변|근처)?|집|집안|실내)에서)?"
    r"(?:지금|갑자기)?"
    r"(?:가스냄새(?:가|는)?(?:좀|많이|계속|강하게)?(?:나요|납니다|나는데요|나는것같아요)|"
    r"가스(?:가)?(?:새요|샙니다|새는것같아요|누출되는것같아요))"
)


def reports_possible_gas_leak(value: str) -> bool:
    for clause, end in re.findall(r"([^.!?。！？,，;；\n]+)([.!?。！？,，;；\n]?)", value):
        if end not in {"?", "？"} and _GAS_REPORT.fullmatch(re.sub(r"\s+", "", clause)):
            return True
    return False


def is_specific_symptom(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).lower()
    return any(
        _TARGET_STATE.search(clause) or _OBSERVABLE.search(clause)
        for clause in re.split(r"[.!?。！？,，;；\n]+", compact)
        if clause
    )
