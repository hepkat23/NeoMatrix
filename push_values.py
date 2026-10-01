import json
import re

def parse_object(value):
    """Parse object value: dict -> deep copy via json, string -> strict json.loads"""
    if isinstance(value, dict):
        # Deep copy using json round-trip for dicts
        return json.loads(json.dumps(value, allow_nan=False))
    elif isinstance(value, str):
        # For strings, we want strict JSON parsing that rejects NaN/Infinity
        try:
            loaded = json.loads(value)
            # Must be a dict - reject all other JSON types
            if not isinstance(loaded, dict):
                raise ValueError("JSON result must be a dict")
            # Check for unquoted NaN/Infinity in numbers (they should not be allowed)
            def check_for_invalid_numbers(obj):
                if isinstance(obj, float):
                    # Check for actual NaN or Infinity values (not strings like "NaN")
                    if json.dumps(obj) in ['NaN', 'Infinity', '-Infinity']:
                        raise ValueError("JSON contains invalid value: NaN or Infinity")
                elif isinstance(obj, dict):
                    for v in obj.values():
                        check_for_invalid_numbers(v)
                elif isinstance(obj, list):
                    for v in obj:
                        check_for_invalid_numbers(v)
            check_for_invalid_numbers(loaded)
            return loaded
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON string: {e}")
    else:
        raise ValueError("Expected dict or JSON string")

def md5_value(value):
    """Validate MD5 value: exactly 32 hex characters, return lowercase"""
    if not isinstance(value, str):
        raise ValueError("MD5 value must be a string")
    if len(value) != 32:
        raise ValueError("MD5 value must be exactly 32 characters")
    if not re.fullmatch(r'^[0-9a-fA-F]{32}$', value):
        raise ValueError("MD5 value must contain only hexadecimal characters")
    return value.lower()

def timestamp_ms(value):
    """Validate timestamp: positive integer or digit-only string, return int"""
    # Handle boolean values specifically - use type() to distinguish from integers
    if type(value) is bool:
        raise ValueError("Timestamp must be positive")
    
    if type(value) is int:
        if value <= 0:
            raise ValueError("Timestamp must be positive")
        return value
    elif isinstance(value, str):
        # Must contain only digits and be > 0
        if not re.fullmatch(r'[0-9]+', value):
            raise ValueError("Timestamp string must contain only digits")
        num_value = int(value)
        if num_value <= 0:
            raise ValueError("Timestamp must be positive")
        return num_value
    else:
        raise ValueError("Timestamp must be an integer or digit-only string")
