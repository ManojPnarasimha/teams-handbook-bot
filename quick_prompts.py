"""Quick-prompt Adaptive Cards: a two-tier topic menu for common HR questions."""

from __future__ import annotations

ROOT_ID = "root"

# Every question below has been verified against the live vector store: it
# retrieves real supporting content and gets a substantive (non-fallback)
# answer from the LLM. Questions that only matched on keyword overlap without
# real supporting content (e.g. Bonafide certificate, NOC letter, hybrid/WFH
# policy) were removed rather than left to dead-end on "couldn't find
# anything relevant." "How do I request an HR letter?" also validated cleanly
# in isolated testing but failed once in live Teams use, so it was dropped
# rather than re-risked — revisit if the underlying cause (possibly the
# gpt-5-mini empty-completion issue, since fixed in llm_client.py) recurs.
# Re-validate before adding new ones back.
CATEGORIES: list[dict] = [
    {
        "id": "docs",
        "title": "Employment documents & letters",
        "prompts": [
            {
                "label": "Experience letter",
                "question": "How do I get an experience letter?",
            },
            {
                "label": "Resignation process",
                "question": "What's the resignation and separation process?",
            }
        ],
    },
    {
        "id": "comp",
        "title": "Compensation, salary & benefits",
        "prompts": [
            {"label": "Insurance policy", "question": "What does my insurance policy cover?"},
            {"label": "Spot awards", "question": "What's the spot awards policy?"},
            {
                "label": "Relocation allowance",
                "question": "What's the relocation and onsite allowance policy?",
            },
        ],
    },
    {
        "id": "loans",
        "title": "Loans & financial support",
        "prompts": [
            {
                "label": "Loan options",
                "question": "What loan or financial support options are available to employees?",
            },
        ],
    },
    {
        "id": "reimb",
        "title": "Reimbursements",
        "prompts": [
            {"label": "Claim reviewers", "question": "Who reviews my reimbursement claims?"},
            {"label": "Processing deadlines", "question": "What are the reimbursement processing deadlines?"},
            {"label": "Team outing limit", "question": "What's the reimbursement limit for a team outing?"},
        ],
    },
    {
        "id": "ld",
        "title": "Learning & development",
        "prompts": [
            {"label": "Certification policy", "question": "What's the certification reimbursement policy?"},
            {
                "label": "Training & development",
                "question": "What's the training and development policy?",
            },
        ],
    },
    {
        "id": "attendance",
        "title": "Attendance & leave",
        "prompts": [
            {"label": "Leave policy", "question": "What's the leave policy?"},
            {"label": "Casual leave", "question": "What's the casual leave policy?"},
            {"label": "Sick leave", "question": "What's the sick leave policy?"},
            {"label": "Earned leave", "question": "How many earned leaves do I get in a year?"},
            {"label": "Maternity leave", "question": "What's the maternity leave policy?"},
            {"label": "Paternity leave", "question": "What's the paternity leave policy?"},
            {"label": "Late working policy", "question": "What's the late working policy?"},
        ],
    },
    {
        "id": "recruit",
        "title": "Recruitment & referrals",
        "prompts": [
            {"label": "Referral policy", "question": "What's the employee referral policy?"},
            {"label": "Recruitment process", "question": "What's the recruitment process?"},
        ],
    },
    {
        "id": "perf",
        "title": "Performance management",
        "prompts": [
            {"label": "Goal setting", "question": "How does goal setting work in performance management?"},
            {
                "label": "Improvement plan",
                "question": "What is the Performance Improvement Plan (PIP) process?",
            },
            {"label": "Promotion policy", "question": "What's the promotion policy?"},
        ],
    },
    {
        "id": "workplace",
        "title": "Workplace & IT policies",
        "prompts": [
            {"label": "Laptop policy", "question": "What's the laptop policy?"},
            {
                "label": "Acceptable use policy",
                "question": "What's the acceptable use policy for company IT resources?",
            },
            {"label": "Social media policy", "question": "What's the social media policy?"},
            {"label": "Privacy policy", "question": "What's the company privacy policy?"},
            {"label": "Drug and alcohol policy", "question": "What's the drug and alcohol policy?"},
        ],
    },
]

_CATEGORIES_BY_ID = {c["id"]: c for c in CATEGORIES}


DEFAULT_GREETING = "Hi! I'm TriVA — pick a topic below or just type your question."


def build_root_card(greeting: str = DEFAULT_GREETING) -> dict:
    """Top-level menu: one button per HR topic."""
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": greeting,
                "wrap": True,
                "weight": "Bolder",
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": category["title"],
                "data": {"quickPromptCategory": category["id"]},
            }
            for category in CATEGORIES
        ],
    }


def build_category_card(category_id: str) -> dict:
    """Submenu for one topic, with the specific questions as imBack buttons."""
    category = _CATEGORIES_BY_ID.get(category_id)
    if category is None:
        return build_root_card()

    actions = [
        {
            "type": "Action.Submit",
            "title": prompt["label"],
            "data": {"msteams": {"type": "imBack", "value": prompt["question"]}},
        }
        for prompt in category["prompts"]
    ]
    actions.append(
        {
            "type": "Action.Submit",
            "title": "< Back to topics",
            "data": {"quickPromptCategory": ROOT_ID},
        }
    )

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": category["title"],
                "wrap": True,
                "weight": "Bolder",
            }
        ],
        "actions": actions,
    }
