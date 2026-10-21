from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class IntakeState(TypedDict):
    steps: list[str]


def test_langgraph_runs_a_stateful_graph():
    # 설치 확인용 최소 그래프. 실제 접수 흐름은 AI 입출력 설계(DS-05) 후 app/ 아래에 만든다
    def collect(state: IntakeState) -> IntakeState:
        return {"steps": [*state["steps"], "collect"]}

    def decide(state: IntakeState) -> IntakeState:
        return {"steps": [*state["steps"], "decide"]}

    graph = StateGraph(IntakeState)
    graph.add_node("collect", collect)
    graph.add_node("decide", decide)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "decide")
    graph.add_edge("decide", END)

    result = graph.compile().invoke({"steps": []})

    assert result["steps"] == ["collect", "decide"]
