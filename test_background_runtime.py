"""Run with calibre-debug -e and NEOMATRIX_TEST_CBR pointing to a local comic."""
import os
import unittest


class BackgroundRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('NEOMATRIX_TEST_CBR'), 'Needs Calibre runtime and a local CBR fixture')
    def test_native_cbr_reader_in_actual_qt_worker(self):
        from qt.core import QCoreApplication
        try:
            from calibre_plugins.frankenboox.background import Worker
            from calibre_plugins.frankenboox.fixed_book import FixedBook
        except ImportError:
            from background import Worker
            from fixed_book import FixedBook
        app=QCoreApplication.instance() or QCoreApplication([])
        def task(status):
            book=FixedBook(os.environ['NEOMATRIX_TEST_CBR'],'CBR')
            self.assertGreater(book.count,0)
            for page in (0,book.count-1):
                self.assertEqual(book.page_from_cfi(book.cfi(page)),page)
            return book.count
        worker=Worker(task,None)
        self.assertGreaterEqual(worker.stackSize(),8*1024*1024)
        worker.finished.connect(app.quit)
        worker.start();app.exec();worker.wait()
        if worker.error:raise worker.error
        self.assertGreater(worker.result,0)


if __name__=='__main__':
    result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(BackgroundRuntimeTests))
    if not result.wasSuccessful():raise SystemExit(1)
