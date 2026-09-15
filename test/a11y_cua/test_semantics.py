import unittest
from analyze import semantic,web_source,sample,episodes
class SemanticsTest(unittest.TestCase):
 def test_host_disambiguation(self):
  for title,expected in [('Calculator','Calculator'),('Settings','WindowsSettings'),('Media Player','WindowsMediaPlayer')]:
   self.assertEqual(semantic({'timestamp':10,'window':{'title':title}},'ApplicationFrameHost.exe',[],[])[0],expected)
 def test_search_not_destination(self):
  self.assertEqual(semantic({'timestamp':10,'window':{'title':'youtube - Google Search - Google Chrome'}},'chrome.exe',[],[])[0],'GoogleSearch')
 def test_click_target_does_not_change_foreground(self):
  self.assertEqual(semantic({'timestamp':10,'window':{'title':'Program Manager'},'element_under_cursor':{'name':'Google Chrome'}},'explorer.exe',[],[])[0],'WindowsShell')
 def test_google_services(self):
  for u,s in [('https://play.google.com/a','GooglePlay'),('https://accounts.google.com','GoogleAccount'),('https://www.google.com/maps/foo','GoogleMaps'),('https://docs.google.com/forms/d/1','GoogleForms')]:self.assertEqual(web_source(u),s)
 def test_no_future_url(self):
  tab={'created':11,'title':'Something','url':'https://youtube.com'}
  self.assertEqual(semantic({'timestamp':10,'window':{'title':'Something - Google Chrome'}},'chrome.exe',[tab],[])[0],'ChromeBrowser')
 def test_bins_and_censor(self):
  for t,c in [(30,'C0'),(30.001,'C1'),(60,'C1'),(60.001,'C2'),(3600,'C6'),(3600.001,'C7')]:self.assertEqual(sample('s','Files',0,t,t,'returned')['class'],c)
  x=sample('s','Files',0,None,4000,'session_end');self.assertEqual(x['censored'],1);self.assertEqual(x['class'],'')
 def test_episode_return_and_censor(self):
  seg=[{'app_name':a,'start':t} for a,t in [('Files',0),('VLC',10),('Files',25)]]
  rows,skip=episodes('s',seg,40)
  self.assertEqual([(x['app_name'],x['reentry_s'],x['censored']) for x in rows],[('Files',15,0),('VLC','',1)])
 def test_unknown_breaks_return(self):
  seg=[{'app_name':a,'start':t} for a,t in [('Files',0),('VLC',10),('<UNKNOWN>',15),('Files',25)]]
  rows,skip=episodes('s',seg,40)
  self.assertEqual(len(rows),1);self.assertEqual(rows[0]['reason'],'unknown_boundary');self.assertEqual(skip,1)
 def test_shell_task_view_is_not_files(self):
  for title in ('Task View','Snap Assist'):
   self.assertEqual(semantic({'timestamp':10,'window':{'title':title}},'explorer.exe',[],[])[0],'WindowsShell')
 def test_edge_pdf_reader(self):
  self.assertEqual(semantic({'timestamp':10,'window':{'title':'Submission 1.pdf - Personal - Microsoft Edge'}},'msedge.exe',[],[])[0],'BrowserPDFReader')
 def test_session_does_not_inherit_candidate(self):
  rows,skip=episodes('next',[{'app_name':'Files','start':100}],200)
  self.assertEqual(rows,[])
if __name__=='__main__':unittest.main()
