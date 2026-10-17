"""Terminal chat for the personal assistant. For the web UI, run assistant.server instead.

Run:  python -m assistant.cli [--auto-recall] [--quiet]

Memory: core facts are always in context, `recall` / `forget` are tools, and a librarian
stores what's worth keeping after each turn. With --auto-recall, the top keyword matches are
also attached to each user message.

Web access: find_sources -> web_search (Brave) -> http_get -> save_source.
Known-good URLs are cached locally so repeat tasks skip the search.

REPL commands: /memories  list everything stored
               /sources   list cached web sources
               /quit      exit
"""

import argparse

from .agent import MODEL, Agent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-recall", action="store_true",
                        help="attach top matching memories to every user message")
    parser.add_argument("--quiet", action="store_true", help="hide tool calls and usage")
    args = parser.parse_args()

    agent = Agent(auto_recall=args.auto_recall)
    if agent.search_error:
        print(f"Web search disabled: {agent.search_error}")
    print(f"Personal assistant ({MODEL}, auto_recall={args.auto_recall}, "
          f"tracing={'on' if agent.tracing else 'off'})")
    print("Type /memories or /sources to inspect storage, /quit to exit.\n")
    verbose = not args.quiet

    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/memories":
            for m in agent.store.all():
                print("  " + m.render())
            continue
        if text == "/sources":
            for s in agent.sources.all():
                print(s.render())
            continue

        in_text = False
        for ev in agent.stream_turn(text):
            kind = ev["type"]
            if kind == "text_start":
                print("\nassistant> " if not in_text else "\n\n", end="", flush=True)
                in_text = True
            elif kind == "text":
                print(ev["delta"], end="", flush=True)
            elif kind == "error":
                print(f"\n  [error] {ev['message']}")
            elif kind == "notice":
                print(f"\n  [notice] {ev['text']}")
            elif kind == "memory":
                print(f"\n  [memory {ev['action']}] {ev['text']}")
            elif not verbose:
                continue
            elif kind == "tool_start":
                print(f"\n  [tool] {ev['name']}({ev['input']})", end="", flush=True)
                in_text = False
            elif kind == "llm" and ev["cost"] is not None:
                print(f"\n  [{ev['role']}] {ev['input_tokens']} in / {ev['output_tokens']} out, "
                      f"${ev['cost']:.4f}, {ev['ms']} ms", end="", flush=True)
                in_text = False
            elif kind == "done":
                print(f"\n  [turn] agent ${ev['agent_cost']:.4f} + judge ${ev['judge_cost']:.4f}"
                      f" + memory ${ev['memory_cost']:.4f} = ${ev['cost']:.4f}, {ev['ms'] / 1000:.1f} s", end="")
        print("\n")


if __name__ == "__main__":
    main()
