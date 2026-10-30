"""FreshTrack evaluation.

    python eval/run_eval.py              # deterministic checks only, no AWS needed
    python eval/run_eval.py --agent      # also run the live agent on Bedrock
    python eval/run_eval.py --agent --baseline   # compare with the same model and no tools
    python eval/run_eval.py --agent --baseline --runs 3   # repeat each agent case to check consistency

Exits non-zero if any deterministic check fails, so CI can run it.

Live runs write eval/results.json. Every run prints a summary.
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
        if "expect_nearest_set" in case:
            ok &= sorted(s["name"] for s in result["nearest_any_day"]) == sorted(case["expect_nearest_set"])
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


def run_guard_cases():
    from freshtrack_agent.agent import tidy_for_sms

    checks = failures = 0
    for case in CASES["guard_cases"]:
        problems = guard.check(case["reply"])
        checks += 1
        if len(problems) != case["expect_problems"]:
            failures += 1
            print(f"  FAIL {case['id']}: expected {case['expect_problems']} problems, got {problems}")
    for case in CASES["tidy_cases"]:
        checks += 1
        if tidy_for_sms(case["text"]) != case["expect"]:
            failures += 1
            print(f"  FAIL {case['id']}: got {tidy_for_sms(case['text'])!r}")
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
    lowered = text.lower()
    if lang == "ar":
        return bool(re.search(r"[؀-ۿ]{3}", text))
    if lang == "zh":
        return len(re.findall(r"[一-鿿]", text)) >= 5
    if lang == "vi":
        return len(re.findall(r"[ăâđêôơưạảấầẩậắằẳặẹẻẽếềểệỉịọỏốồổộớờởợụủứừửựỳỵỷỹ]", lowered)) >= 5
    stopwords = {
        "es": r"el|la|los|las|para|hoy|puede|llame|llama|abierto|comida|de|en|al|tienes|hay|con",
        "fr": r"le|la|les|vous|pour|est|ce|de|ouvert|appelez",
        "sw": r"na|ya|kwa|leo|wa|piga|chakula|saa|hadi",
        "en": r"the|is|you|to|and|call|open",
    }[lang]
    return len(re.findall(rf"\b({stopwords})\b", lowered)) >= 3


def run_agent_cases(model_id, baseline, runs=1):
    from strands import Agent
    from strands.models import BedrockModel

    from freshtrack_agent.agent import NAVIGATOR_PROMPT, FreshTrack

    rows = []
    for case, run in ((c, r) for c in CASES["agent_cases"] for r in range(runs)):
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
        lowered = reply.lower()
        checks = {
            "site_hit": any(t.lower() in lowered for t in case.get("must_mention_any", [])) or "must_mention_any" not in case,
            "requirement_met": any(t.lower() in lowered for t in case.get("must_include_any", [])) or "must_include_any" not in case,
            "nothing_forbidden": not any(t.lower() in lowered for t in case.get("must_not_include", [])),
            "final_guard_pass": not guard.check(reply, tools.now()),
            "language_ok": language_ok(reply, case["lang"]),
        }
        rows.append({
            "id": case["id"],
            "run": run + 1,
            "passed": all(checks.values()),
            **checks,
            "lang": case["lang"],
            "reply": reply,
            "seconds": seconds,
            "draft_guard_pass": not trace["guard_problems"],
            "guard_problems": trace["guard_problems"],
            "invented_items": len(trace["guard_problems"]),
            "rewritten": trace["rewritten"],
            "fell_back": trace.get("fell_back", False),
            "tool_calls": trace["tool_calls"],
            "fk_grade": fk_grade(reply) if case["lang"] == "en" else None,
            "words": len(reply.split()),
        })
        failed = [k for k, v in checks.items() if not v]
        print(f"  {case['id']} #{run + 1}: {'pass' if not failed else 'FAIL ' + ', '.join(failed)}"
              f"{' (rewritten)' if rows[-1]['rewritten'] else ''} {seconds}s")
    tools.set_now(None)
    return rows


def summarize(rows):
    n = len(rows)
    grades = [r["fk_grade"] for r in rows if r["fk_grade"] is not None]
    by_case = {}
    for r in rows:
        by_case.setdefault(r["id"], []).append(r["passed"])
    return {
        "replies": n,
        "cases": len(by_case),
        "replies_passing_every_check": f"{sum(r['passed'] for r in rows)}/{n}",
        "cases_passing_on_every_run": f"{sum(all(v) for v in by_case.values())}/{len(by_case)}",
        "right_site_named": f"{sum(r['site_hit'] for r in rows)}/{n}",
        "safety_or_case_requirement_met": f"{sum(r['requirement_met'] and r['nothing_forbidden'] for r in rows)}/{n}",
        "replies_with_wrong_time_or_phone_before_guard": f"{sum(not r['draft_guard_pass'] for r in rows)}/{n}",
        "replies_with_wrong_time_or_phone_delivered": f"{sum(not r['final_guard_pass'] for r in rows)}/{n}",
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
    parser.add_argument("--runs", type=int, default=1, help="times to run each agent case")
    parser.add_argument("--model", default=os.environ.get("FRESHTRACK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"))
    args = parser.parse_args()

    results = {"model": args.model, "run_at": dt.datetime.now().isoformat(timespec="seconds")}
    print("Schedule logic (hand-checked expected answers):")
    results["schedule"] = run_schedule_cases()
    print(f"  {results['schedule']['passed']}/{results['schedule']['checks']} passed")
    print("Search tool (offline):")
    results["search"] = run_search_cases()
    print(f"  {results['search']['passed']}/{results['search']['checks']} passed")
    print("Guard and SMS tidy (offline):")
    results["guard"] = run_guard_cases()
    print(f"  {results['guard']['passed']}/{results['guard']['checks']} passed")
    offline_ok = all(results[k]["passed"] == results[k]["checks"] for k in ("schedule", "search", "guard"))

    if args.agent:
        print(f"FreshTrack agent ({args.model}):")
        rows = run_agent_cases(args.model, baseline=False, runs=args.runs)
        results["agent"] = {"summary": summarize(rows), "rows": rows}
        if args.baseline:
            print("Baseline, same model and prompt with no tools:")
            base = run_agent_cases(args.model, baseline=True, runs=args.runs)
            results["baseline"] = {"summary": summarize(base), "rows": base}
        print("\n" + json.dumps({k: v["summary"] for k, v in results.items() if isinstance(v, dict) and "summary" in v}, indent=2))

    # Only live runs replace the published results, so an offline run never erases them.
    if args.agent:
        with open(os.path.join(ROOT, "eval", "results.json"), "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2, ensure_ascii=False)
    if not offline_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
