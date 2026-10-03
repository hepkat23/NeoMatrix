import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from record_cleanup import remove_records,restore_records,record_token,conditional_where,read_backup,eligible_records,write_backup
from storage_cleanup import Scan,BookRecord,CleanupError
ROW={'id':'1','uuid':'a'*32,'title':"O'Brien: 日本語",'location':'/storage/emulated/0/Books/Old.epub','progress':'3/10','readingStatus':'1','extraAttributes':'{"position":"chapter:2"}','description':None}
class Device:
 def __init__(self):
  self.serial='TEST';self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
  self.db.execute('CREATE TABLE Metadata(id INTEGER PRIMARY KEY,uuid TEXT,title TEXT COLLATE NOCASE,location TEXT,progress TEXT,readingStatus INTEGER,extraAttributes TEXT,description TEXT)')
  self.insert_record(ROW);self.writes=0;self.paused=False;self.exists=False;self.race=None;self.supported=True
 def metadata(self):return [{k:None if v is None else str(v) for k,v in dict(r).items()} for r in self.db.execute('SELECT * FROM Metadata ORDER BY id')]
 def ensure_connected(self):pass
 def check_record_provider(self):
  if not self.supported:raise CleanupError('Unsupported firmware')
 def book_status(self,paths):return 'P' if self.exists else 'M'
 def pause_reader(self):self.paused=True
 def delete_record(self,row):
  if self.race:self.db.execute(self.race)
  cur=self.db.execute('DELETE FROM Metadata WHERE '+conditional_where(row));self.writes+=1;return cur.rowcount
 def insert_record(self,row):self.db.execute('INSERT INTO Metadata('+','.join(row)+') VALUES('+','.join('?' for k in row)+')',list(row.values()))
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'backup.json';self.dev=Device()
  self.rows=self.dev.metadata();self.scan=Scan('TEST',[],[],self.rows,[BookRecord('Book',(ROW['location'],),'M','Missing')]);self.tokens={record_token('TEST',self.rows[0])}
 def tearDown(self):self.tmp.cleanup()
 def remove(self):return remove_records(self.dev,self.scan,self.tokens,self.path)
 def test_remove_restore_exact_identity_and_position(self):
  self.assertEqual(self.remove()['removed_records'],1);self.assertEqual(self.dev.metadata(),[])
  self.assertEqual(restore_records(self.dev,self.path)['restored_records'],1);self.assertEqual(self.dev.metadata(),self.rows)
  self.assertEqual(restore_records(self.dev,self.path)['already_present'],1)
 def test_current_book_protected(self):
  self.scan.book_records[0]=BookRecord('Book',(), 'P','On BOOX')
  with self.assertRaises(CleanupError):self.remove()
  self.assertFalse(self.path.exists());self.assertEqual(self.dev.writes,0)
 def test_book_reappears_at_old_path(self):
  self.dev.exists=True
  with self.assertRaises(CleanupError):self.remove()
  self.assertFalse(self.path.exists());self.assertEqual(self.dev.writes,0)
 def test_changed_progress_preserved(self):
  self.dev.db.execute("UPDATE Metadata SET progress='8/10'")
  with self.assertRaises(CleanupError):self.remove()
  self.assertFalse(self.path.exists());self.assertEqual(self.dev.metadata()[0]['progress'],'8/10')
 def test_conditional_delete_catches_race(self):
  self.dev.race="UPDATE Metadata SET progress='9/10'"
  with self.assertRaises(CleanupError):self.remove()
  self.assertEqual(self.dev.metadata()[0]['progress'],'9/10');self.assertTrue(self.path.exists())
 def test_wrong_device(self):
  self.dev.serial='OTHER'
  with self.assertRaises(CleanupError):self.remove()
  self.assertFalse(self.path.exists())
 def test_unverified_firmware_no_writes(self):
  self.dev.supported=False
  with self.assertRaises(CleanupError):self.remove()
  self.assertEqual(self.dev.writes,0);self.assertFalse(self.path.exists())
 def test_existing_backup_not_overwritten(self):
  self.path.write_text('Existing')
  with self.assertRaises(FileExistsError):self.remove()
  self.assertEqual(self.dev.writes,0);self.assertEqual(self.path.read_text(),'Existing')
 def test_corrupt_backup_rejected(self):
  self.remove();b=json.loads(self.path.read_text());b['payload']['records'][0]['title']='Changed';self.path.write_text(json.dumps(b))
  with self.assertRaises(CleanupError):restore_records(self.dev,self.path)
  self.assertEqual(self.dev.metadata(),[])
 def test_restore_conflict_preserved(self):
  self.remove();self.dev.insert_record(dict(ROW,uuid='b'*32,title='Newer book'))
  with self.assertRaises(CleanupError):restore_records(self.dev,self.path)
  self.assertEqual(self.dev.metadata()[0]['title'],'Newer book')
 def test_restore_wrong_device(self):
  self.remove();self.dev.serial='OTHER'
  with self.assertRaises(CleanupError):restore_records(self.dev,self.path)
  self.assertEqual(self.dev.metadata(),[])
 def test_duplicate_ids_and_unknown_selection_protected(self):
  with self.assertRaises(CleanupError):eligible_records(self.scan,{'unknown'})
  self.scan.metadata.append(self.rows[0]);self.scan.book_records.append(self.scan.book_records[0])
  with self.assertRaises(CleanupError):self.remove()
 def test_partial_removal_can_be_restored(self):
  second=dict(ROW,id='2',uuid='b'*32,title='Second book')
  self.dev.insert_record(second);self.rows=self.dev.metadata()
  self.scan.metadata=self.rows;self.scan.book_records.append(BookRecord('Second',(second['location'],),'M','Missing'))
  self.tokens={record_token('TEST',row) for row in self.rows}
  real_delete=self.dev.delete_record
  def fail_second(row):
   if row['id']=='2':raise CleanupError('Device disconnected')
   return real_delete(row)
  self.dev.delete_record=fail_second
  with self.assertRaises(CleanupError):self.remove()
  self.assertEqual(len(self.dev.metadata()),1)
  self.assertEqual(restore_records(self.dev,self.path)['restored_records'],1)
  self.assertEqual(self.dev.metadata(),self.rows)
 def test_whitespace_and_multiline_removal_restore(self):
  self.dev.db.execute('UPDATE Metadata SET title=?,description=?,extraAttributes=?',
                      (' title with a trailing space ', 'line one\nline two', ' {"x":"NULL, id=inside value"} '))
  self.rows=self.dev.metadata();self.scan.metadata=self.rows
  self.tokens={record_token('TEST',self.rows[0])}
  self.assertEqual(self.remove()['removed_records'],1)
  restore_records(self.dev,self.path);self.assertEqual(self.dev.metadata(),self.rows)
 def test_legacy_partial_backup_retains_unmodified_whitespace_record(self):
  second=dict(ROW,id='2',uuid='b'*32,title=' Second book ')
  self.dev.insert_record(second)
  original_first=self.dev.metadata()[0]
  legacy_second=dict(self.dev.metadata()[1],title='Second book')
  write_backup(self.path,'TEST',[original_first,legacy_second])
  self.dev.delete_record(original_first)
  result=restore_records(self.dev,self.path)
  self.assertEqual(result,{'restored_records':1,'already_present':1})
  self.assertEqual(self.dev.metadata()[1]['title'],' Second book ')
 def test_legacy_backup_does_not_overwrite_new_progress(self):
  self.rows=self.dev.metadata();write_backup(self.path,'TEST',self.rows)
  self.dev.db.execute("UPDATE Metadata SET progress='9/10'")
  with self.assertRaises(CleanupError):restore_records(self.dev,self.path)
  self.assertEqual(self.dev.metadata()[0]['progress'],'9/10')
 def test_rejection_message_identifies_record_and_partial_count(self):
  self.dev.delete_record=lambda row:0
  with self.assertRaisesRegex(CleanupError, 'O.Brian|O.Brie') as error:self.remove()
  self.assertIn('Removed: 0 of 1 selected records',str(error.exception))
 def test_case_change_cannot_match_case_insensitive_column(self):
  self.dev.db.execute("UPDATE Metadata SET title=lower(title)")
  self.assertEqual(self.dev.delete_record(self.rows[0]),0)
if __name__=='__main__':unittest.main()
