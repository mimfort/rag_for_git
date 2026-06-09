from __future__ import annotations
from langgraph.graph import StateGraph, START, END

from reviewer.agent.state import ReviewState, Deps
from reviewer.agent import nodes

def build_graph(deps: Deps, publish: bool = True):
    """Собрать граф ревью.

    publish=False — узел publish не добавляется (assemble → END); используется
    для dry-run: ревью считается, но не публикуется в VCS.
    """
    b = StateGraph(ReviewState)
    b.add_node("plan", nodes.plan_node)
    b.add_node("analyze", nodes.make_analyze_node(deps))
    b.add_node("verify", nodes.make_verify_node(deps))
    b.add_node("assemble", nodes.make_assemble_node(deps))
    b.add_edge(START, "plan")
    b.add_conditional_edges("plan", nodes.fan_out, ["analyze"])
    b.add_edge("analyze", "verify")
    if getattr(deps, "synthesizer", None) is not None:
        b.add_node("synthesize", nodes.make_synthesize_node(deps))
        b.add_edge("verify", "synthesize")
        b.add_edge("synthesize", "assemble")
    else:
        b.add_edge("verify", "assemble")
    if publish:
        b.add_node("publish", nodes.make_publish_node(deps))
        b.add_edge("assemble", "publish")
        b.add_edge("publish", END)
    else:
        b.add_edge("assemble", END)
    return b.compile()
