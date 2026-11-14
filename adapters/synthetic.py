"""Synthetic (non-confidential) email / chat / voice-transcript generators."""
import random

# label -> phrase templates. {amt} {freq} {date} are filled per unit.
PHRASES = {
    "PAYMENT_VERIFICATION": ["I already paid ${amt}", "I made a payment of ${amt} last week",
                             "I transferred ${amt} on {date}"],
    "DEBT_DISPUTE": ["I dispute the balance", "I do not owe this amount", "this debt is not mine"],
    "PAYMENT_ARRANGEMENT": ["I request {freq} instalments", "I would like to pay it off {freq}",
                            "can we set up a {freq} payment plan"],
    "HARDSHIP_NOTIFICATION": ["I lost my job last month", "I am in financial hardship right now"],
    "ACCOUNT_BALANCE_REQUEST": ["please send me a statement of my current balance",
                                "how much do I owe in total"],
    "COMPLAINT": ["I want to lodge a complaint about how I was contacted"],
    "CEASE_CONTACT_REQUEST": ["stop calling me at work", "do not contact me by phone again"],
    "DETAILS_UPDATE": ["my new address is 12 Example Street", "please update my phone number to 0400 000 000"],
}
LABELS = list(PHRASES)
SUBJECTS = ["Re: account notice", "Question about my account", "Payment query", "Urgent: reference {ref}",
            "Regarding letter received", "Hardship and payments", "Account {ref}"]
AGENT_LINES = ["Thanks for getting in touch. How can I help?", "Can you tell me more about that?",
               "Understood, I will note that down.", "Is there anything else you need?"]
FILLERS = ["Um,", "Well,", "Yeah,", "So,", "Look,"]
DATES = ["Monday", "3 March", "yesterday", "the 15th"]


def _phrases(rng: random.Random, n: int) -> list[str]:
    labels = rng.sample(LABELS, n)
    out = []
    for label in labels:
        text = rng.choice(PHRASES[label])
        out.append(text.format(amt=f"{rng.choice([250, 480, 1200, 2000, 3500]):,}",
                               freq=rng.choice(["monthly", "weekly", "fortnightly"]),
                               date=rng.choice(DATES)))
    return out


def _sentence(phrases: list[str], rng: random.Random) -> str:
    parts = [p[0].upper() + p[1:] if i == 0 else p for i, p in enumerate(phrases)]
    joiners = [". ", " and ", ", but "]
    text = parts[0]
    for p in parts[1:]:
        text += rng.choice(joiners) + (p[0].upper() + p[1:] if text.endswith(". ") else p)
    return text + "."


def make_email(rng: random.Random) -> dict:
    ref = rng.randint(10000, 99999)
    body = f"Hello,\n\n{_sentence(_phrases(rng, rng.randint(1, 3)), rng)}\n\nRegards,\nCustomer {ref}"
    payload = {"sender": f"customer{ref}@example.invalid",
               "subject": rng.choice(SUBJECTS).format(ref=ref), "body": body}
    if rng.random() < 0.2:
        payload["attachments"] = [{"filename": "payment_receipt.txt",
                                   "content": f"SYNTHETIC RECEIPT\nReference {ref}\n"}]
    return payload


def make_chat(rng: random.Random) -> dict:
    phrases = _phrases(rng, rng.randint(1, 3))
    messages = [{"speaker": "Agent", "text": rng.choice(AGENT_LINES), "timestamp": "00:00"}]
    t = 15
    for phrase in phrases:
        messages.append({"speaker": "Customer", "text": phrase[0].upper() + phrase[1:] + ".",
                         "timestamp": f"{t // 60:02d}:{t % 60:02d}"})
        t += 20
        messages.append({"speaker": "Agent", "text": rng.choice(AGENT_LINES),
                         "timestamp": f"{t // 60:02d}:{t % 60:02d}"})
        t += 25
    return {"messages": messages}


def make_voice(rng: random.Random) -> dict:
    phrases = _phrases(rng, rng.randint(1, 3))
    segments = [{"speaker": "Agent", "text": rng.choice(AGENT_LINES), "start_s": 0.0}]
    t = 6.0
    for phrase in phrases:
        segments.append({"speaker": "Customer", "text": f"{rng.choice(FILLERS)} {phrase}.", "start_s": t})
        t += 9.0
        segments.append({"speaker": "Agent", "text": rng.choice(AGENT_LINES), "start_s": t})
        t += 7.0
    return {"segments": segments}


GENERATORS = {"email": make_email, "chat": make_chat, "voice": make_voice}


def generate(channel: str, seed: int) -> dict:
    return GENERATORS[channel](random.Random(seed))
