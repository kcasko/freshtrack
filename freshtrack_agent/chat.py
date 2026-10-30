"""Talk to FreshTrack in the terminal.

    python -m freshtrack_agent.chat
    python -m freshtrack_agent.chat --now "2026-11-03 17:45"   # pin the clock for a demo
    python -m freshtrack_agent.chat --show-guard                # print the guard trace
"""

import argparse
import datetime as dt

from . import tools
from .agent import MODEL_ID, FreshTrack


def main():
    parser = argparse.ArgumentParser(description="FreshTrack food help agent")
    parser.add_argument("--now", help='Pretend it is this local time, "YYYY-MM-DD HH:MM"')
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--show-guard", action="store_true")
    args = parser.parse_args()

    if args.now:
        tools.set_now(dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M"))
    bot = FreshTrack(model_id=args.model)
    print(f"FreshTrack ({tools.now():%a %b %d %I:%M %p}). Type your message, or 'quit'.\n")

    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if message.lower() in ("quit", "exit"):
            break
        if not message:
            continue
        print(f"\nfreshtrack> {bot.ask(message)}\n")
        if args.show_guard:
            trace = bot.last_trace
            print(f"  [tools: {', '.join(trace['tool_calls']) or 'none'} | guard: {trace['guard_problems'] or 'passed'}"
                  f"{' | rewritten by fact-checker' if trace['rewritten'] else ''}]\n")


if __name__ == "__main__":
    main()
