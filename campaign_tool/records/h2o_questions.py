"""Typed question set asked of the local H2O model beside the records rules (records-v1).

Wording is copied from the deep plan's Q-table. Q1 (per-page OCR) and Q4 (open-request
match) are not part of this set. The model's answers are advice only.
"""

VERSION = "records-v1"

QUESTIONS = {
    "doc_type": {
        "type": "choice",
        "instructions": "What kind of message is this, from the point of view of the person who sent the "
                        "public-records request?",
        "criteria": {
            "acknowledgement": "confirms the request was received or logged; no decision yet",
            "extension": "says the agency needs more time and extends the response date",
            "denial": "refuses to release the requested records, fully, citing an exemption",
            "fee_notice": "asks for payment or confirmation of a cost before release",
            "records": "releases records (all or some of them)",
            "no_records": "says no responsive records exist",
            "clarification": "asks us to clarify or narrow the request",
            "other": "anything else (newsletters, portal notices, errors)",
        },
    },
    "deadline_stated": {
        "type": "choice",
        "instructions": "Does the message state a specific date, or a number of days, by which the agency or "
                        "the requester must act?",
        "criteria": {
            "yes": "a date or number of days is stated",
            "no": "no specific date or number of days",
        },
    },
    "completeness": {
        "type": "choice",
        "instructions": "Does this message release all of the requested records, part of them, or none?",
        "criteria": {
            "complete": "all requested records are released, nothing withheld or redacted",
            "partial_withheld": "some records are released but some are withheld, redacted or still to come",
            "fully_withheld": "nothing is released; everything is withheld",
            "not_applicable": "no release decision in this message",
        },
    },
    "privacy_present": {
        "type": "noul",
        "instructions": "The message contains personal information about a private individual (for example a "
                        "home address, date of birth, driver licence number or a named private vehicle owner) "
                        "that must not be published.",
        "criteria": {
            "true": "private personal information is present",
            "false": "no private personal information",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How soon must the requester act on this message?",
        "criteria": ["no action needed", "within a month", "within a week", "within two days"],
    },
    "followup_needed": {
        "type": "choice",
        "instructions": "Because of this message, must the requester act (pay, clarify, appeal or challenge a "
                        "withholding, or chase missing records)?",
        "criteria": {
            "yes": "the requester must act",
            "no": "nothing for the requester to do now",
        },
    },
}

# Asked on every chunk of a long text; the rest are asked on the first chunk only.
CHUNKED = ("doc_type", "urgency")
