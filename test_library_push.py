import unittest
from library_push import newest_position, ensure_pull_ready, PUSH_KEY

class DB:
    def __init__(self, marker):self.marker=marker
    def get_custom_book_data(self,key):return {1:self.marker} if key==PUSH_KEY else {}

class LibraryPositions(unittest.TestCase):
    def test_newest_saved_position_wins(self):
        result=newest_position([{'cfi':'epubcfi(old)','epoch':1}, {'pos':'epubcfi(new)','timestamp':'2026-10-01T00:00:00+00:00'}])
        self.assertEqual(result['cfi'],'epubcfi(new)')
    def test_tied_different_positions_blocked(self):
        with self.assertRaises(ValueError):newest_position([{'cfi':'epubcfi(a)','epoch':2},{'cfi':'epubcfi(b)','epoch':2}])
    def test_invalid_times_not_used(self):
        for when in (True,float('inf'),float('nan'),0,-1,None):
            with self.subTest(when=when),self.assertRaises(ValueError):newest_position([{'cfi':'epubcfi(a)','epoch':when}])
    def test_timezone_required(self):
        with self.assertRaises(ValueError):newest_position([{'pos':'epubcfi(a)','timestamp':'2026-10-01T00:00:00'}])
    def test_unopened_push_cannot_import_stale_fraction(self):
        identity={'metadata_uuid':'copy-one','document_id':'doc-one','path':'/sdcard/a.epub'}
        db=DB({'status':'written','identity':identity,'written_last_access_ms':2000})
        for time in (1000,2000):
            with self.subTest(time=time),self.assertRaises(ValueError):ensure_pull_ready(db,1,{'boox_identity':identity,'progress':{'last_access_ms':time}})
        ensure_pull_ready(db,1,{'boox_identity':identity,'progress':{'last_access_ms':2001}})
    def test_other_volume_does_not_clear_pending_push(self):
        db=DB({'status':'written','identity':{'metadata_uuid':'volume-one'},'written_last_access_ms':2000})
        with self.assertRaises(ValueError):ensure_pull_ready(db,1,{'boox_identity':{'metadata_uuid':'volume-two'},'progress':{'last_access_ms':9000}})
    def test_combined_copies_use_exact_pushed_copy(self):
        db=DB({'status':'pending','identity':{'metadata_uuid':'copy-one'},'written_last_access_ms':2000})
        state={'boox_identity':{'metadata_uuid':'copy-two'},'source_states':[{'boox_identity':{'metadata_uuid':'copy-one'},'progress':{'last_access_ms':2001}}]}
        ensure_pull_ready(db,1,state)
    def test_old_behavior_when_no_push_marker(self):
        ensure_pull_ready(DB(None),1,{})

if __name__=='__main__':unittest.main()
