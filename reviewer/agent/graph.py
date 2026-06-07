from __future__ import annotations
from langgraph.graph import StateGraph, START, END

from reviewer.agent.state import ReviewState, Deps
from reviewer.agent import nodes

def build_graph(deps: Deps):
    b = StateGraph(ReviewState)
    b.add_node("plan", nodes.plan_node)
    b.add_node("analyze", nodes.make_analyze_node(deps))
    b.add_node("verify", nodes.make_verify_node(deps))
    b.add_node("assemble", nodes.make_assemble_node(deps))
    b.add_node("publish", nodes.make_publish_node(deps))
    b.add_edge(START, "plan")
    b.add_conditional_edges("plan", nodes.fan_out, ["analyze"])
    b.add_edge("analyze", "verify")
    b.add_edge("verify", "assemble")
    b.add_edge("assemble", "publish")
    b.add_edge("publish", END)
    return b.compile()
