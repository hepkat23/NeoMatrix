import json,shlex,sqlite3,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from storage_cleanup import read_metadata_exact,metadata_columns,CleanupError,Scan,BookRecord
from record_cleanup import record_token,remove_records,restore_records,write_backup
class Provider:
 def __init__(self,db):self.db=db
 def __call__(self,command):
  args=shlex.split(command);projection=args[args.index('--projection')+1]
  sql='SELECT '+projection.replace(':',',')+' FROM Metadata'
  cursor=self.db.execute(sql);names=[col[0] for col in cursor.description];rows=cursor.fetchall()
  if not rows:return 'No result found.\n'
  return ''.join('Row: '+str(i)+' '+', '.join(key+'='+('NULL' if value is None else str(value)) for key,value in zip(names,row))+'\n' for i,row in enumerate(rows))
class Tests(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:')
  self.db.execute('CREATE TABLE Metadata(id INTEGER PRIMARY KEY,uuid TEXT,title TEXT,description TEXT,extraAttributes TEXT)')
  self.query=Provider(self.db)
 def add(self,title=' title ',description=None):
  self.db.execute('INSERT INTO Metadata VALUES(?,?,?,?,?)',(1,'a'*32,title,description,' {"note":"x, id=still inside JSON"} '))
 def test_whitespace_roundtrip(self):
  self.add(' Francesca Woodman : retrospective ', ' \tprivate note\nsecond line\t ')
  row=read_metadata_exact(self.query)[0]
  self.assertEqual(row['title'],' Francesca Woodman : retrospective ')
  self.assertEqual(row['description'],' \tprivate note\nsecond line\t ')
  self.assertEqual(row['extraAttributes'],' {"note":"x, id=still inside JSON"} ')
 def test_null_is_distinct_from_literal_null(self):
  self.add(description='NULL');self.assertEqual(read_metadata_exact(self.query)[0]['description'],'NULL')
  self.db.execute('UPDATE Metadata SET description=NULL');self.assertIsNone(read_metadata_exact(self.query)[0]['description'])
 def test_empty_and_unicode_values(self):
  self.add('日本語 😀','');row=read_metadata_exact(self.query)[0]
  self.assertEqual(row['title'],'日本語 😀');self.assertEqual(row['description'],'')
 def test_empty_table_restore_reader(self):
  self.assertIn('uuid',metadata_columns(self.query));self.assertEqual(read_metadata_exact(self.query,allow_empty=True),[])
  with self.assertRaises(CleanupError):read_metadata_exact(self.query)
 def test_corrupt_hex_rejected(self):
  with self.assertRaises(CleanupError):read_metadata_exact(lambda cmd:'Row: 0 id=31, uuid=GG\n',columns=('id','uuid'))
 def test_incomplete_projection_rejected(self):
  with self.assertRaises(CleanupError):read_metadata_exact(lambda cmd:'Row: 0 id=31\n',columns=('id','uuid'))
 def test_unsafe_schema_rejected(self):
  with self.assertRaises(CleanupError):metadata_columns(lambda cmd:'Row: 0 nm_columns=id,uuid,bad-column, nm_count=1\n')
if __name__=='__main__':unittest.main()
