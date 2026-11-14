"""Quick model health/latency check: python -m workers.check_model [--runs 3]
Runs real inference on synthetic units with the configured backend and reports latency and output validity."""
import argparse
import sys
import time

import config
from adapters import synthetic
from intake import schema
from workers import model_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=config.MODEL_TIMEOUT)
    args = parser.parse_args()
    client = model_client.make_client()
    print(f"backend={client.backend} model={client.model} url={config.MODEL_URL} timeout={args.timeout}s")
    print(f"server: {client.server_version() or 'not reachable / unknown'}")
    if client.simulated:
        print("mock backend: this is NOT real inference")
    times, bad = [], 0
    for i in range(args.runs):
        payload = synthetic.generate("email", 1000 + i)
        segments = [{"id": "MSG-T", "speaker": "Customer", "text": payload["body"]}]
        prompt = schema.build_user_prompt(segments, "email")
        t0 = time.perf_counter()
        try:
            out = client.generate(schema.SYSTEM_PROMPT, prompt, args.timeout)
        except model_client.LLMError as exc:
            print(f"run {i + 1}: FAILED {exc.code}: {exc} after {time.perf_counter() - t0:.1f}s")
            bad += 1
            continue
        dt = time.perf_counter() - t0
        times.append(dt)
        try:
            result = schema.validate_output(out, segments)
            verdict = f"valid, {len(result['intents'])} intent(s), warnings={result['validation']['warnings']}"
        except schema.InvalidOutput as exc:
            verdict, bad = f"INVALID output ({exc})", bad + 1
        print(f"run {i + 1}: {dt:.1f}s  {verdict}")
    if times:
        avg = sum(times) / len(times)
        print(f"average {avg:.1f}s per unit per worker -> one worker ~{60 / avg:.1f} units/min; "
              f"350 units in 600 s needs 35 units/min")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
