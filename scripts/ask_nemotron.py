"""Chat with the NVIDIA-hosted Nemotron model.

One-shot:
    python scripts/ask_nemotron.py "your prompt here"

Interactive REPL (keeps conversation history until you exit):
    python scripts/ask_nemotron.py
"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

MODEL = "nvidia/nemotron-3-super-120b-a12b"


def make_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["ALGO_NVIDIA_API_KEY"],
        base_url="https://integrate.api.nvidia.com/v1",
    )


def ask(client: OpenAI, messages: list[dict]) -> str:
    stream = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=1,
        top_p=0.95,
        max_tokens=16384,
        stream=True,
    )

    reasoning_started = False
    answer_started = False
    content = ""

    for chunk in stream:
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            if not reasoning_started:
                print("\033[2m", end="")  # dim
                reasoning_started = True
            print(reasoning, end="", flush=True)
        if delta.content:
            if reasoning_started and not answer_started:
                print("\033[0m\n")  # reset dim, blank line before answer
            answer_started = True
            print(delta.content, end="", flush=True)
            content += delta.content
    if reasoning_started and not answer_started:
        print("\033[0m", end="")
    print()
    return content


def repl() -> None:
    client = make_client()
    messages: list[dict] = []
    print(f"Nemotron REPL ({MODEL}).")
    print("Type 'exit', 'quit', or Ctrl+C to leave, 'reset' to clear history.")
    print()

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break
        if user_input.lower() == "reset":
            messages = []
            print("(history cleared)\n")
            continue

        messages.append({"role": "user", "content": user_input})
        try:
            reply = ask(client, messages)
        except Exception as exc:  # noqa: BLE001 - surface any API error and keep the REPL alive
            print(f"\n[error] {exc}\n")
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": reply})
        print()


def main() -> None:
    prompt = " ".join(sys.argv[1:])
    if not prompt:
        repl()
        return

    client = make_client()
    ask(client, [{"role": "user", "content": prompt}])


if __name__ == "__main__":
    main()
