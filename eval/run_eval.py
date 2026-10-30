"""FreshTrack evaluation.

    python eval/run_eval.py              # deterministic checks only, no AWS needed
    python eval/run_eval.py --agent      # also run the live agent on Bedrock
    python eval/run_eval.py --agent --baseline   # compare with the same model and no tools

Writes eval/results.json and prints a summary table.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from freshtrack_agent import data, guard, tools  # noqa: E402

CASES = json.load(open(os.path.join(ROOT, "eval", "cases.json"), encoding="utf-8"))


def parse_now(text):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M")


def run_schedule_cases():
    checks = failures = 0
    for case in CASES["schedule_cases"]:
        moment = parse_now(case["now"])
        for name, expected in case["expect"].items():
            actual = data.status_at(data.find_site(name), moment)["status"]
            checks += 1
            if actual != expected:
                failures += 1
                print(f"  FAIL {case['id']}: {name} expected {expected}, got {actual}")
    return {"checks": checks, "passed": checks - failures}


def run_search_cases():
    os.environ["FRESHTRACK_OFFLINE"] = "1"
    checks = failures = 0
    for case in CASES["search_cases"]:
        tools.set_now(parse_now(case["now"]))
        result = tools.find_food_help._tool_func(case["location"], case["need"], 6)
        ok = True
        if "expect_available_count" in case:
            ok &= len(result["open_now_or_later_today"]) == case["expect_available_count"]
        if "expect_nearest_names" in case:
            ok &= [s["name"] for s in result["nearest_any_day"]] == case["expect_nearest_names"]
        if "expect_far" in case:
            ok &= bool(result.get("far_from_sites")) == case["expect_far"]
        if "expect_first_available" in case:
            ok &= result["open_now_or_later_today"][0]["name"] == case["expect_first_available"]
        checks += 1
        if not ok:
            failures += 1
            print(f"  FAIL {case['id']}: {json.dumps(result)[:300]}")
    tools.set_now(None)
    os.environ.pop("FRESHTRACK_OFFLINE")
    return {"checks": checks, "passed": checks - failures}


def syllables(word):
    word = word.lower().strip(".,!?;:'\"()")
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups) - (1 if word.endswith("e") and len(groups) > 1 else 0)
    return max(1, count)


def fk_grade(text):
    """Flesch-Kincaid grade level, English only."""
    sentences = max(1, len(re.findall(r"[.!?]+(\s|$)", text)))
    words = re.findall(r"[A-Za-z][A-Za-z'&-]*", text)
    if not words:
        return None
    return round(0.39 * len(words) / sentences + 11.8 * sum(syllables(w) for w in words) / len(words) - 15.59, 1)


def language_ok(text, lang):
    if lang == "ar":
        return bool(re.search(r"[؀-ۿ]{3}", text))
    if lang == "es":
        return len(re.findall(r"\b(el|la|los|para|hoy|puede|llame|abierto|comida|de)\b", text.lower())) >= 3
    return len(re.findall(r"\b(the|is|you|to|and|call|open)\b", text.lower())) >= 3


def run_agent_cases(model_id, baseline):
    from strands import Agent
    from strands.models import BedrockModel

    from freshtrack_agent.agent import NAVIGATOR_PROMPT, FreshTrack

    rows = []
    for case in CASES["agent_cases"]:
        tools.set_now(parse_now(case["now"]))
        started = time.time()
        if baseline:
            prompt = NAVIGATOR_PROMPT.format(now=tools.now().strftime("%A %B %d, %I:%M %p"))
            plain = Agent(model=BedrockModel(model_id=model_id, temperature=0.2), system_prompt=prompt, callback_handler=None)
            reply = str(plain(case["message"])).strip()
            trace = {"guard_problems": guard.check(reply, tools.now()), "rewritten": False, "tool_calls": []}
        else:
            bot = FreshTrack(model_id=model_id)
            reply = bot.ask(case["message"])
            trace = bot.last_trace
        seconds = round(time.time() - started, 1)
        rows.append({
            "id": case["id"],
            "lang": case["lang"],
            "reply": reply,
            "seconds": seconds,
            "site_hit": any(term.lower() in reply.lower() for term in case["must_mention_any"]),
            "draft_guard_pass": not trace["guard_problems"],
            "final_guard_pass": not guard.check(reply, tools.now()),
            "invented_items": len(trace["guard_problems"]),
            "rewritten": trace["rewritten"],
            "fell_back": trace.get("fell_back", False),
            "tool_calls": trace["tool_calls"],
            "language_ok": language_ok(reply, case["lang"]),
            "fk_grade": fk_grade(reply) if case["lang"] == "en" else None,
            "words": len(reply.split()),
        })
        print(f"  {case['id']}: hit={rows[-1]['site_hit']} guard={rows[-1]['final_guard_pass']} "
              f"rewritten={rows[-1]['rewritten']} {seconds}s")
    tools.set_now(None)
    return rows


def summarize(rows):
    n = len(rows)
    grades = [r["fk_grade"] for r in rows if r["fk_grade"] is not None]
    return {
        "cases": n,
        "right_site_named": f"{sum(r['site_hit'] for r in rows)}/{n}",
        "replies_with_invented_time_or_phone_before_guard": f"{sum(not r['draft_guard_pass'] for r in rows)}/{n}",
        "replies_with_invented_time_or_phone_delivered": f"{sum(not r['final_guard_pass'] for r in rows)}/{n}",
        "rewritten_by_fact_checker": sum(r["rewritten"] for r in rows),
        "safe_fallbacks": sum(r["fell_back"] for r in rows),
        "reply_in_users_language": f"{sum(r['language_ok'] for r in rows)}/{n}",
        "median_reading_grade_en": sorted(grades)[len(grades) // 2] if grades else None,
        "median_words": sorted(r["words"] for r in rows)[n // 2],
        "median_seconds": sorted(r["seconds"] for r in rows)[n // 2],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", action="store_true", help="run live agent cases on Amazon Bedrock")
    parser.add_argument("--baseline", action="store_true", help="also run the same model with no tools")
    parser.add_argument("--model", default=os.environ.get("FRESHTRACK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"))
    args = parser.parse_args()

    results = {"model": args.model, "run_at": dt.datetime.now().isoformat(timespec="seconds")}
    print("Schedule logic (hand-checked expected answers):")
    results["schedule"] = run_schedule_cases()
    print(f"  {results['schedule']['passed']}/{results['schedule']['checks']} passed")
    print("Search tool (offline):")
    results["search"] = run_search_cases()
    print(f"  {results['search']['passed']}/{results['search']['checks']} passed")

    if args.agent:
        print(f"FreshTrack agent ({args.model}):")
        rows = run_agent_cases(args.model, baseline=False)
        results["agent"] = {"summary": summarize(rows), "rows": rows}
        if args.baseline:
            print("Baseline, same model and prompt with no tools:")
            base = run_agent_cases(args.model, baseline=True)
            results["baseline"] = {"summary": summarize(base), "rows": base}
        print("\n" + json.dumps({k: v["summary"] for k, v in results.items() if isinstance(v, dict) and "summary" in v}, indent=2))

    with open(os.path.join(ROOT, "eval", "results.json"), "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
