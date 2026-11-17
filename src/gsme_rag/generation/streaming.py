"""Incremental structured output, never a raw-token bypass of citation checks."""

import json


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Answer JSON contains a duplicate key")
        value[key] = item
    return value


DECODER = json.JSONDecoder(object_pairs_hook=unique_object)


def strict_loads(text):
    return json.loads(text, object_pairs_hook=unique_object)


def completed_claims(text):
    """Read complete top-level claim objects from an incomplete JSON document.

    A JSON decoder handles escaped quotes/brackets, not a regex searching for
    the word 'claims' inside untrusted strings. Final schema/status validation
    is still mandatory. At most eight completed claims can be released.
    """
    index, status, claims, seen = 0, None, [], set()

    def whitespace(position):
        while position < len(text) and text[position].isspace():
            position += 1
        return position

    index = whitespace(index)
    if index == len(text):
        return status, claims
    if text[index] != "{":
        raise ValueError("Answer stream is not a JSON object")
    index += 1
    try:
        while True:
            index = whitespace(index)
            if index == len(text) or text[index] == "}":
                return status, claims
            key, index = DECODER.raw_decode(text, index)
            if key not in {"status", "claims"} or key in seen:
                raise ValueError("Answer stream contains an unknown or duplicate field")
            seen.add(key)
            index = whitespace(index)
            if index == len(text):
                return status, claims
            if text[index] != ":":
                raise ValueError("Invalid answer field separator")
            index = whitespace(index + 1)
            if key == "status":
                status, index = DECODER.raw_decode(text, index)
                if status not in {"answered", "insufficient_evidence", "clarification_needed"}:
                    raise ValueError("Unknown streamed answer status")
            else:
                if index == len(text):
                    return status, claims
                if text[index] != "[":
                    raise ValueError("Answer claims must be an array")
                index = whitespace(index + 1)
                while index < len(text) and text[index] != "]":
                    item, index = DECODER.raw_decode(text, index)
                    if not isinstance(item, dict):
                        raise ValueError("Each answer claim must be an object")
                    claims.append(item)
                    if len(claims) > 8:
                        raise ValueError("Answer stream exceeds eight claims")
                    index = whitespace(index)
                    if index == len(text):
                        return status, claims
                    if text[index] == ",":
                        index = whitespace(index + 1)
                    elif text[index] != "]":
                        raise ValueError("Invalid claim separator")
                if index == len(text):
                    return status, claims
                index += 1
            index = whitespace(index)
            if index == len(text) or text[index] == "}":
                return status, claims
            if text[index] != ",":
                raise ValueError("Invalid answer object separator")
            index += 1
    except json.JSONDecodeError:
        # A partial string/object is normal during provider streaming. Invalid
        # final JSON is rejected by strict_loads and the full schema afterwards.
        return status, claims
