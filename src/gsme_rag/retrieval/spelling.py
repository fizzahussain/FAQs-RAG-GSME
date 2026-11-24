"""Auditable query-only typo assistance; evidence and technical values stay exact."""

import re

# Demonstrated/common customer-word misspellings. Unknown tokens, acronyms,
# URLs, code, numbers, flavor names and negation are never guessed.
CORRECTIONS = {
    "foudned":"founded", "founed":"founded", "foundded":"founded",
    "semicondutor":"semiconductor", "semicondcutor":"semiconductor", "semiconducter":"semiconductor",
    "manufaturing":"manufacturing", "manufactring":"manufacturing", "manufacturign":"manufacturing",
    "technolgy":"technology", "technlogy":"technology", "technolgies":"technologies",
    "fabelss":"fabless", "fablss":"fabless", "fablesss":"fabless",
    "differnce":"difference", "differece":"difference", "betwen":"between",
    "postponned":"postponed", "postponeing":"postponing", "commmercial":"commercial",
    "suppport":"support", "proccess":"process", "packging":"packaging",
    "assemb ly":"assembly", "tapout":"tapeout", "tapeot":"tapeout",
    "tapeuot":"tapeout", "scheduel":"schedule", "schedual":"schedule",
    "scedule":"schedule", "scheduels":"schedules", "scedules":"schedules",
    "deadine":"deadline", "deadines":"deadlines", "prelimnary":"preliminary",
    "tape out":"tapeout", "tape outs":"tapeouts", "tape-out":"tapeout", "tape-outs":"tapeouts",
}
PATTERN = re.compile(r"\b(?:"+"|".join(re.escape(k) for k in sorted(CORRECTIONS,key=len,reverse=True))+r")\b", re.I)
PROTECTED = re.compile(r'''https?://\S+|`[^`]*`|"[^"]*"|'[^']*'|\b[A-Za-z]*[0-9][A-Za-z0-9.+-]*\b|\bflavo[u]?r\b.*''', re.I)


def correct_query(question):
    protected = [m.span() for m in PROTECTED.finditer(question)]
    changes = []
    def replace(match):
        if any(start <= match.start() < end for start,end in protected):
            return match.group()
        before = match.group()
        after = CORRECTIONS[before.casefold()]
        changes.append({"from":before,"to":after})
        return after
    corrected = PATTERN.sub(replace,question)
    return corrected, changes
