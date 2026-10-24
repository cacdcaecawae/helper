"""Synthetic browser regression; never imports the native bridge or starts WeChat."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
TOKEN='synthetic-ui-token'


class Fixture:
    def __init__(self):
        self.sent=[]
        self.messages=[
            {'id':1,'revision':1,'chat_id':'wxid_ui_peer','sender_id':'wxid_ui_peer','sender_name':'周屿','content':'这是一条合成消息，仅供界面测试。','date':'2026-10-23','time':'09:30','is_group':False,'direction':'incoming'},
            {'id':2,'revision':2,'chat_id':'wxid_ui_peer','sender_id':'wxid_ui_self','sender_name':'我','content':'<script>window.uiXss=1</script> 中文🙂','date':'2026-10-23','time':'09:31','is_group':False,'direction':'outgoing'},
            {'id':3,'revision':3,'chat_id':'fixture_group@chatroom','sender_id':'wxid_ui_member','sender_name':'成员甲','content':'稍后讨论一下新的界面。','date':'2026-10-23','time':'09:28','is_group':True,'direction':'incoming'}]

    def get(self,path):
        name=urlsplit(path).path.removeprefix('/api/')
        if name=='robots':return {'robots':[{'id':'default','account':'wxid_ui_self','name':'合成账号','state':'running','connected':True}]}
        if name=='accounts':return {'accounts':[{'pid':1234,'wxid':'wxid_ui_self','name':'合成账号','state':'running','connected':True,'selectable':True,'robot_id':'default'}]}
        if name=='state':return {'state':'running','connected':True,'account':'wxid_ui_self','pid':1234,'plugins':[{'name':'echo','enabled':False,'active':False,'description':'合成插件','errors':0}]}
        if name=='config':return {'groups':[],'chat_allowlist':[],'plugins':{'echo':{'enabled':False}},'llm':{},'tts':{},'text_send_delay':{'enabled':False,'characters_per_group':10,'min_seconds':0,'max_seconds':0}}
        if name=='logs':return {'rows':[],'cursor':0}
        if name=='messages':return {'rows':self.messages,'cursor':len(self.messages),'stream_id':'synthetic-stream'}
        if name=='chats':return {'chats':[{'chat_id':'wxid_ui_peer','name':'周屿','is_group':False},{'chat_id':'fixture_group@chatroom','name':'课题组','is_group':True}]}
        if name=='send-connection':return {'session_id':'synthetic-session'}
        if name=='group-members':return {'session_id':'synthetic-session','members':[{'wxid':'wxid_ui_member','nickname':'成员甲','role':'admin'}]}
        if name=='send-status':return {'status':'sent'}
        raise ValueError(name)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def reply(self,code,value,kind='application/json; charset=utf-8'):
        data=json.dumps(value,ensure_ascii=False).encode() if not isinstance(value,bytes) else value
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)))
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        path=urlsplit(self.path).path
        files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        if path in files:
            name,kind=files[path];data=(ROOT/'webui'/name).read_text(encoding='utf8').replace('__HELPER_TOKEN__',TOKEN).encode()
            return self.reply(200,data,kind)
        if self.headers.get('X-Helper-Token')!=TOKEN:return self.reply(403,{'error':'token'})
        try:self.reply(200,self.server.fixture.get(self.path))
        except ValueError:self.reply(404,{'error':'unknown route'})
    def do_POST(self):
        if self.headers.get('X-Helper-Token')!=TOKEN:return self.reply(403,{'error':'token'})
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if urlsplit(self.path).path!='/api/send-text':return self.reply(400,{'error':'Unexpected mutation in UI test'})
        if data.get('session_id')!='synthetic-session':return self.reply(409,{'error':'session changed'})
        self.server.fixture.sent.append(data)
        self.reply(200,{'ok':True})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--screenshots',action='store_true');args=parser.parse_args()
    from playwright.sync_api import sync_playwright,expect
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.fixture=Fixture()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    artifacts=ROOT/'.runtime/ui-check';artifacts.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':900},reduced_motion='reduce')
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            expect(page.locator('#send-content')).to_be_enabled()
            page.locator('#send-content').fill('尚未选择会话')
            page.locator('#send-content').press('Enter')
            assert not server.fixture.sent and not page.locator('#chat-send-dialog').is_visible()
            page.get_by_role('button',name='打开会话 周屿',exact=True).click()
            expect(page.locator('#chat-heading')).to_have_text('周屿')
            expect(page.locator('#send-content')).to_be_enabled()
            assert page.locator('.hc-layout').bounding_box()['height'] >= 830
            assert page.locator('#chat-timeline script').count()==0
            assert page.evaluate('() => window.uiXss') is None
            if args.screenshots:page.screenshot(path=str(artifacts/'desktop.png'))
            page.locator('#send-content').fill('测试文字，不接触真实微信')
            page.locator('#send-text').click();expect(page.locator('#chat-send-dialog')).to_be_visible()
            assert not server.fixture.sent
            page.locator('#chat-confirm-cancel').click();assert not server.fixture.sent
            page.locator('#send-content').press('Enter');expect(page.locator('#chat-send-dialog')).to_be_visible()
            page.locator('#chat-confirm-send').click()
            expect(page.locator('#send-content')).to_have_value('')
            assert len(server.fixture.sent)==1 and server.fixture.sent[0]['chat_id']=='wxid_ui_peer'
            page.locator('#send-content').fill('会话甲草稿')
            page.get_by_role('button',name='打开会话 课题组',exact=True).click()
            expect(page.locator('#send-content')).to_have_value('')
            page.locator('#chat-mention-toggle').click()
            page.locator('[data-mention="wxid_ui_member"]').check()
            page.locator('#send-content').fill('群内合成消息')
            page.locator('#send-text').click();page.locator('#chat-confirm-send').click()
            expect(page.locator('#send-content')).to_have_value('')
            assert len(server.fixture.sent)==2 and server.fixture.sent[-1]['at_list']==['wxid_ui_member']
            page.get_by_role('button',name='打开会话 周屿',exact=True).click()
            expect(page.locator('#send-content')).to_have_value('会话甲草稿')
            page.locator('#send-text').click()
            page.evaluate("() => { document.querySelector('#send-content').value = 'changed'; }")
            page.locator('#chat-confirm-send').click();assert len(server.fixture.sent)==2
            expect(page.locator('#send-status')).to_contain_text('未发送')
            for name in ['overview','plugins','groups','models','logs','chat']:
                page.locator('[data-page="'+name+'"]').click()
                expect(page.locator('#page-'+name)).to_be_visible()
            page.locator('#theme-toggle').click()
            expect(page.locator('html')).to_have_attribute('data-theme','dark')
            if args.screenshots:page.screenshot(path=str(artifacts/'dark.png'))
            page.locator('#theme-toggle').click()
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#chat-back').click()
            page.get_by_role('button',name='打开会话 周屿',exact=True).click()
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
            detail=page.locator('.hc-conversation').bounding_box()
            assert abs(detail['x'])<1 and detail['width']==390 and detail['height']>=780
            toolbar=page.locator('.robot-toolbar').bounding_box()
            assert toolbar['y']<60 and toolbar['x']+toolbar['width']<=390
            expect(page.locator('#send-text')).to_be_enabled()
            if args.screenshots:page.screenshot(path=str(artifacts/'mobile.png'))
            page.locator('#chat-back').click();page.locator('#nav-toggle').click()
            page.locator('[data-page="overview"]').click();expect(page.locator('#page-overview')).to_be_visible()
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
            assert not errors,errors
            browser.close()
        print(json.dumps({'passed':True,'realWechatAccessed':False,'syntheticSends':len(server.fixture.sent),'confirmation':True,'mentionFlow':True,'draftIsolation':True,'staleConfirmationBlocked':True,'managementPages':True,'darkMode':True,'mobile':True}))
    finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':main()
