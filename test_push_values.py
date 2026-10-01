import unittest
from push_values import parse_object, md5_value, timestamp_ms

class TestPushValues(unittest.TestCase):
    
    def test_parse_object_dict_deep_copy(self):
        """Test that parse_object performs deep copy for dicts"""
        original = {"a": 1, "b": {"c": 2}}
        result = parse_object(original)
        self.assertEqual(result, original)
        # Ensure it's a deep copy
        result["a"] = 999
        self.assertEqual(original["a"], 1)
    
    def test_parse_object_valid_json_string(self):
        """Test parse_object with valid JSON string"""
        json_str = '{"a": 1, "b": {"c": 2}}'
        result = parse_object(json_str)
        self.assertEqual(result, {"a": 1, "b": {"c": 2}})
    
    def test_parse_object_invalid_json_string(self):
        """Test parse_object rejects invalid JSON strings"""
        with self.assertRaises(ValueError):
            parse_object('{"a": 1, "b":}')
    
    def test_parse_object_rejects_non_dict_results(self):
        """Test parse_object rejects JSON that doesn't decode to dict"""
        # Reject arrays
        with self.assertRaises(ValueError):
            parse_object('[]')
        
        # Reject null
        with self.assertRaises(ValueError):
            parse_object('null')
            
        # Reject numbers
        with self.assertRaises(ValueError):
            parse_object('1')
        
        # Reject booleans  
        with self.assertRaises(ValueError):
            parse_object('true')
            
        # Reject NaN/Infinity in nested values
        with self.assertRaises(ValueError):
            parse_object('{"a": NaN}')
        with self.assertRaises(ValueError):
            parse_object('{"a": [Infinity]}')
        self.assertEqual(parse_object('{"a": "NaN"}'), {'a': 'NaN'})
    
    def test_md5_value_case_normalization(self):
        """Test md5_value normalizes case"""
        result = md5_value("ABCDEF0123456789ABCDEF0123456789")
        self.assertEqual(result, "abcdef0123456789abcdef0123456789")
    
    def test_md5_value_invalid_inputs(self):
        """Test md5_value rejects invalid inputs"""
        # Wrong length
        with self.assertRaises(ValueError):
            md5_value("1234567890123456789012345678901")  # 31 chars
        
        with self.assertRaises(ValueError):
            md5_value("123456789012345678901234567890123")  # 33 chars
        
        # Non-hex characters
        with self.assertRaises(ValueError):
            md5_value("GGGGGG0123456789ABCDEF0123456789")
        
        # Not a string
        with self.assertRaises(ValueError):
            md5_value(123)
    
    def test_timestamp_ms_positive_integers(self):
        """Test timestamp_ms accepts positive integers"""
        self.assertEqual(timestamp_ms(123), 123)
        self.assertEqual(timestamp_ms(1), 1)
    
    def test_timestamp_ms_positive_strings(self):
        """Test timestamp_ms accepts positive digit-only strings"""
        self.assertEqual(timestamp_ms("123"), 123)
        self.assertEqual(timestamp_ms("1"), 1)
    
    def test_timestamp_ms_rejects_false_true_zero(self):
        """Test timestamp_ms rejects boolean values and zero"""
        with self.assertRaises(ValueError):
            timestamp_ms(True)
        
        with self.assertRaises(ValueError):
            timestamp_ms(False)
            
        with self.assertRaises(ValueError):
            timestamp_ms(0)
        
        with self.assertRaises(ValueError):
            timestamp_ms("0")
    
    def test_timestamp_ms_rejects_floats_and_negatives(self):
        """Test timestamp_ms rejects floats and negative numbers"""
        with self.assertRaises(ValueError):
            timestamp_ms(-1)
        
        with self.assertRaises(ValueError):
            timestamp_ms("-1")
            
        with self.assertRaises(ValueError):
            timestamp_ms(1.5)
        
        with self.assertRaises(ValueError):
            timestamp_ms("1.5")
    
    def test_timestamp_ms_rejects_non_digit_strings(self):
        """Test timestamp_ms rejects non-digit strings"""
        with self.assertRaises(ValueError):
            timestamp_ms("abc")
        
        with self.assertRaises(ValueError):
            timestamp_ms("123abc")


if __name__ == '__main__':
    unittest.main()
