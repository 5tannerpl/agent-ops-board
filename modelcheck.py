from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="local-demo"
)

system_prompt = """
/no_think

You classify debt collection emails.
Return ALL explicitly expressed intents.

Labels:
DEBT_DISPUTE: Debt or balance is disputed.
PAYMENT_VERIFICATION: Sender claims a payment was made.
PAYMENT_ARRANGEMENT: Requests installments or a payment plan.
SETTLEMENT_REQUEST: Explicitly proposes a reduced amount.
LEGAL_STATUS_INQUIRY: Asks about legal proceedings.
OTHER: None of the above.

Rules:
- Multiple labels are allowed.
- A reported payment requires PAYMENT_VERIFICATION.
- Installments are not a settlement request.
- Never infer unsupported intents.
- Output JSON only: {"intents": ["LABEL"]}
"""

response = client.chat.completions.create(
    model="Qwen3-4B-Instruct-2507 Q8_0",
    messages=[
        {
            "role": "system",
            "content": system_prompt
        },
        {
            "role": "user",
            "content": (
                "I dispute the $8,500 debt. "
                "I already paid $2,000 and "
                "want monthly instalments."
            )
        }
    ],
    temperature=0
)

print(response.choices[0].message.content)