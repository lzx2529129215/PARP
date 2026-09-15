import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from user_events_plan import build, classify


def row(app, text):
    return dict(app_name=app, event1=text)


class WorkbookMappingTests(unittest.TestCase):
    def test_complete_import_preserves_group_provenance_and_unknown_boundaries(self):
        plan = build(Path('/home/lzx/Desktop/user_events合并.xlsx'))
        self.assertEqual(len(plan['events']), 2615)
        self.assertEqual(len(plan['groups']), 12)
        self.assertEqual([e['excel_row'] for e in plan['events']], list(range(2,2617)))
        for g in plan['groups']:
            es=[e for e in plan['events'] if e['source_dataset_id']==g['source_dataset_id']]
            self.assertEqual(es[0]['source_delay_s'],0)
            self.assertTrue(all(e['source_file'] and e['source_row_number'] for e in es))
        desktop=[e for e in plan['events'] if e['operation']=='desktop']
        self.assertTrue(desktop)
        self.assertTrue(all(e['runtime_app_id'] is None and e['vocab_id']==31 for e in desktop))

    def test_mislabelled_launch_and_cad_are_not_file_browser_actions(self):
        self.assertEqual(classify(row('文件管理器','从应用中心冷启动WPS'),{})['target_app_key'],'WPS')
        state={}
        self.assertEqual(classify(row('文件管理器','鼠标双击打开 cad_1.dwg'),state)['operation'],'skip')
        self.assertEqual(classify(row('文件管理器','点击平移'),state)['operation'],'skip')
        classify(row('文件管理器','关闭中望CAD应用'),state)
        self.assertEqual(classify(row('文件管理器','向下滑动第1次浏览文管文档'),state)['operation'],'scroll')

    def test_meetings_are_not_mislabeled_as_chat_coverage(self):
        for app in ['飞书','腾讯会议','虚拟机','小艺']:
            self.assertEqual(classify(row(app,'点击开始会议'),{})['disposition'],'skip')

    def test_desktop_boundary_takes_precedence_over_labeled_application(self):
        e=classify(row('WPS','进入任务中心'),{})
        self.assertIsNone(e['target_app_key'])
        self.assertEqual(e['operation'],'desktop')

    def test_upload_and_install_are_not_replaced_with_fake_success(self):
        for app,text in [('浏览器','浏览器腾讯文档上传本地PPT文件'),('应用市场','点击“安装”'),
                         ('应用中心','点击卸载应用开心消消乐')]:
            self.assertEqual(classify(row(app,text),{})['operation'],'skip')


if __name__=='__main__':unittest.main()
