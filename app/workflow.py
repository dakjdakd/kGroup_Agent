from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .service import ConversationService


class GraphState(TypedDict, total=False):
    customer_id: str
    message_id: str
    text: str
    result: dict


def build_graph(service: ConversationService):
    graph = StateGraph(GraphState)

    def process(state: GraphState) -> GraphState:
        result = service.handle_message(state["customer_id"], state["message_id"], state["text"])
        return {"result": result.as_dict()}

    graph.add_node("process_conversation", process)
    graph.add_edge(START, "process_conversation")
    graph.add_edge("process_conversation", END)
    return graph.compile()
