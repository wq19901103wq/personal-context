"""Browser regressions using a disposable store; no workers or model calls.

Run: python -m unittest discover -s tests -p test_page_connection.py
Playwright and its Chromium browser are optional development dependencies.
"""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from zoneinfo import ZoneInfo

from app import Assistant
from presentation.task_web import task_server

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'Playwright is not installed')
class PageConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = Assistant(Path(temporary.name) / 'assistant.db')
        self.task = self.app.store.create(description='连接测试待办')
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.app.store.create_event(title='连接测试日程', scheduled_date=today)
        server = task_server(self.app, 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.root = f'http://127.0.0.1:{server.server_port}'
        self.calendar = self.root + '/calendar/' + self.app.store.calendar_token()
        self.detail = self.root + '/task/' + self.app.store.task_token(self.task['id'])
        self.context = self.browser.new_context(viewport=dict(width=390, height=844))
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.clock.install()

    def test_mobile_recovers_from_initial_proxy_failure_and_preserves_offline_draft(self):
        calls = 0

        def initial_failure(route):
            nonlocal calls
            calls += 1
            if calls == 1:
                route.fulfill(status=502, content_type='text/html', body='Bad Gateway')
            else:
                route.continue_()

        self.page.route('**/data', initial_failure)
        self.page.goto(self.calendar)
        expect(self.page.locator('#page-error')).to_contain_text('自动重连')
        self.page.clock.run_for(16000)
        expect(self.page.locator('#sync-status')).to_contain_text('已同步')
        expect(self.page.locator('.cal-title', has_text='连接测试日程')).to_be_visible()
        self.page.locator('#new-event').click()
        self.page.locator('#event-title').fill('尚未保存的草稿')
        self.context.set_offline(True)
        expect(self.page.locator('#page-error')).to_contain_text('网络已断开')
        expect(self.page.locator('.cal-title', has_text='连接测试日程')).to_be_visible()
        self.context.set_offline(False)
        expect(self.page.locator('#sync-status')).to_contain_text('已同步')
        expect(self.page.locator('#event-title')).to_have_value('尚未保存的草稿')
        self.assertEqual(self.errors, [])

    def test_task_initial_load_recovers_and_uncertain_write_is_not_repeated(self):
        reads = 0
        writes = 0

        def connection_fault(route):
            nonlocal reads, writes
            if route.request.method == 'POST':
                writes += 1
                # Commit on the real fixture server, then lose only the response.
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort('connectionreset')
            else:
                reads += 1
                if reads == 1:
                    route.fulfill(status=502, content_type='text/html', body='Bad Gateway')
                else:
                    route.continue_()

        self.page.route('**/data', connection_fault)
        self.page.goto(self.detail)
        expect(self.page.locator('#error')).to_contain_text('自动重连')
        self.page.clock.run_for(6000)
        expect(self.page.locator('#title')).to_have_text('连接测试待办')
        self.page.locator('#editor > summary').click()
        self.page.locator('#description').fill('待办编辑草稿')
        self.app.store.update(self.task['id'], {'notes': '后台已变化'})
        self.page.clock.run_for(3000)
        expect(self.page.locator('#description')).to_have_value('待办编辑草稿')
        self.page.locator('#editor > summary').click()
        self.page.get_by_role('button', name='完成待办', exact=True).click()
        expect(self.page.locator('#error')).to_contain_text('先刷新查看结果')
        self.page.locator('#refresh').click()
        expect(self.page.locator('#status')).to_contain_text('已完成')
        self.page.clock.run_for(12000)
        self.assertEqual(writes, 1)
        self.assertEqual(self.app.store.get(self.task['id'])['status'], 'done')
        self.assertEqual(self.errors, [])

    def test_a_stalled_refresh_times_out_without_accumulating_requests(self):
        self.page.goto(self.calendar)
        expect(self.page.locator('#sync-status')).to_contain_text('已同步')
        held = []
        self.page.route('**/data', lambda route: held.append(route))
        self.page.locator('#reload').click()
        self.page.wait_for_function("document.querySelector('#reload') !== null")
        self.page.locator('#reload').click(click_count=4)
        self.assertEqual(len(held), 1)
        self.page.clock.run_for(15000)
        expect(self.page.locator('#page-error')).to_contain_text('自动重连')
        # One initial read and one coalesced trailing read; timers never overlap them.
        self.assertLessEqual(len(held), 2)
        self.page.unroute('**/data')
        for route in held:
            route.abort()
        self.page.locator('#reload').click()
        expect(self.page.locator('#sync-status')).to_contain_text('已同步')
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
