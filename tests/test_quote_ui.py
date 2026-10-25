"""用隔离的无头浏览器验证引用入口；页面不连接真实管理台、不发送消息。"""
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
BROWSER = next((p for p in (
    Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))/'Google/Chrome/Application/chrome.exe',
    Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',
) if p.is_file()), None)


@unittest.skipUnless(BROWSER is not None, '需要本机 Chrome/Edge 才执行隔离浏览器验证')
class QuoteUITests(unittest.TestCase):
    def test_quote_selection_preview_confirmation_and_cancel_without_sending(self):
        script = (ROOT/'webui/app.js').read_text(encoding='utf-8')
        initialization = '(async()=>{renderState();try{await loadRobots();await loadConfig();await loadAccounts();await poll();}catch(error){serviceDisconnected();toast(error.message,true);}setInterval(poll,2000);})();'
        self.assertTrue(initialization in script, '前端初始化入口已变化，需更新隔离探针')
        probe = r'''
const checks={};
try{
  // 只注入合成消息，关闭真实 API 初始化；没有发送按钮的确认动作。
  state={connected:true,state:'running',account:'wxid_ui_bot'};serviceAvailable=true;
  chatLoadKey=robot;chatRows=[{chat_id:'12345678@chatroom',name:'合成群'}];
  $('#send-chat').innerHTML='<option value="12345678@chatroom">合成群</option>';
  sendSession='ui-test';sendLoading=false;
  messages=[{id:1,type:1,chat_id:'12345678@chatroom',is_group:true,sender_name:'群友甲',content:'原话 <b>不是HTML</b>',direction:'incoming'},
            {id:2,type:1,chat_id:'12345678@chatroom',is_group:true,sender_name:'机器人',content:'机器人自己的话',direction:'outgoing'}];
  renderChatWorkspace();renderSendState();
  checks.outgoing_visible=document.querySelectorAll('.hc-message.outgoing').length===1;
  checks.buttons=document.querySelectorAll('[data-quote-index]').length===2;
  checks.no_html=document.querySelector('.hc-bubble b')===null;
  checks.group_quote_disabled=document.querySelector('[data-quote-index="0"]').disabled;
  document.querySelector('[data-quote-index="0"]').click();
  checks.no_fake_quote_preview=$('#chat-quote-preview').classList.contains('hidden');
  $('#send-content').value='真的假的';$('#send-content').dispatchEvent(new Event('input'));
  checks.composed_plain_reply_only=composeSendText()==='真的假的';
  $('#send-form').requestSubmit();
  checks.confirmation=$('#chat-send-dialog').open&&$('#chat-confirm-text').textContent===composeSendText();
  $('#chat-confirm-cancel').click();
  checks.cancel_preserves_draft=$('#send-content').value==='真的假的'&&!$('#chat-send-dialog').open;
  $('#chat-quote-cancel').click();
  checks.quote_cancel=$('#chat-quote-preview').classList.contains('hidden')&&composeSendText()==='真的假的';
  chatRows=[{chat_id:'wxid_ui_peer',name:'私聊朋友'}];
  $('#send-chat').innerHTML='<option value="wxid_ui_peer">私聊朋友</option>';
  messages=[{id:3,type:1,chat_id:'wxid_ui_peer',sender_id:'wxid_ui_peer',sender_name:'朋友',content:'原话',
             message_id:'3918779855605843848',quote_token:'a'.repeat(64),direction:'incoming'},
            {id:4,type:1,chat_id:'wxid_ui_peer',direction:'outgoing',content:'readable projection',quote:{version:1,reply:'111哈哈哈',
             reference:{sender_name:'朋友',content:'原话 <b>不是HTML</b>',message_id:'3918779855605843848'}}}];
  renderChatWorkspace();renderSendState();
  document.querySelector('[data-quote-index="0"]').click();
  checks.native_id_string=nativeQuoteId()==='3918779855605843848';
  checks.native_snapshot_token=chatQuotes.get(robot+':wxid_ui_peer').source.token==='a'.repeat(64);
  checks.legacy_row_without_token_disabled=!canNativeQuote({id:10,type:1},'wxid_ui_peer');
  checks.native_reply_not_flattened=composeSendText()==='真的假的';
  checks.native_preview=$('#chat-quote-text').textContent.includes('微信原生引用');
  checks.native_card_rendered=!!document.querySelector('.hc-native-reference')&&document.querySelector('.hc-native-reference b')===null;
  $('#send-form').requestSubmit();
  checks.native_confirmation=$('#chat-send-dialog').open&&$('#chat-confirm-text').textContent.includes('[微信原生引用]');
  $('#chat-confirm-cancel').click();$('#chat-quote-cancel').click();
  messages.push({id:'h55',history:true,chat_id:'wxid_ui_peer',sender_id:'wxid_ui_peer',sender_name:'朋友',
                 content:'没有微信消息ID的旧文字',quote_token:'b'.repeat(64),direction:'incoming'});
  renderChatWorkspace();renderSendState();
  document.querySelector('[data-quote-index="2"]').click();
  checks.history_keeps_local_reference=chatQuotes.get(robot+':wxid_ui_peer').source.id==='h55'&&
      chatQuotes.get(robot+':wxid_ui_peer').source.history===true&&
      chatQuotes.get(robot+':wxid_ui_peer').source.token==='b'.repeat(64);
  checks.history_does_not_invent_id=nativeQuoteId()===''&&composeSendText()==='真的假的';
  chatQuotes.set(robot+':wxid_ui_peer',{native:false,author:'朋友',content:'old text fallback'});
  renderSendState();
  checks.stale_fallback_blocks_send=$('#send-text').disabled&&composeSendText()==='真的假的';
  $('#chat-quote-cancel').click();
  checks.no_send=pendingSend===null;
}catch(error){checks.error=String(error);}
document.body.setAttribute('data-helper-ui-test',JSON.stringify(checks));
'''
        script = script.replace(initialization, probe)
        page = (ROOT/'webui/index.html').read_text(encoding='utf-8').replace('__HELPER_TOKEN__','synthetic-ui-token')
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/app.js'):
                    raw, kind = script.encode(), 'application/javascript'
                elif self.path.startswith('/style.css'):
                    raw, kind = (ROOT/'webui/style.css').read_bytes(), 'text/css'
                elif self.path.startswith('/api/'):
                    self.send_error(500,'Test must not call a real API');return
                else:
                    raw, kind = page.encode(), 'text/html'
                self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8')
                self.end_headers();self.wfile.write(raw)
            def log_message(self,*_):pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='helper-quote-ui-') as folder:
                result = subprocess.run([str(BROWSER),'--headless=new','--disable-gpu','--no-first-run',
                    '--disable-background-networking','--disable-component-update','--no-default-browser-check',
                    '--user-data-dir='+folder,'--virtual-time-budget=1200','--dump-dom',
                    'http://127.0.0.1:'+str(server.server_port)+'/'],capture_output=True,timeout=25,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                output = result.stdout.decode('utf-8',errors='replace')
                match = re.search(r'data-helper-ui-test="([^"]*)"',output)
                self.assertIsNotNone(match,'浏览器没有完成引用交互探针')
                checks = json.loads(html.unescape(match.group(1)))
                self.assertTrue(checks,checks)
                self.assertNotIn('error',checks,checks)
                self.assertTrue(all(checks.values()),checks)
        finally:
            server.shutdown();server.server_close();thread.join(2)
