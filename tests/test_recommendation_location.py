"""Location must be understood for the target date before place discovery."""

import asyncio
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from personal_assistant.places import origin_options, select_candidates
from personal_assistant.proactive import ProactiveWorker
from personal_assistant.store import TaskStore


def at(day, hour):
    return datetime(2026, 11, day, hour, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class RecommendationLocationTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TaskStore(Path(self.directory.name) / 'assistant.db')
        self.context = dict(current_location=None, requested_categories=['exhibition'],
            historical_locations=[dict(id='work', field='workplace_location', value='南京办公楼')],
            memories=[], chat_profiles=[], dh_facts=[])

    def test_recent_overnight_and_return_are_available_before_any_map_search(self):
        trip = self.store.create_event(title='青岛住宿：海边旅馆',
            notes='南京商场集合，之后去青岛入住，返程未定。', location='南京集合商场',
            starts_at=at(6, 21), ends_at=at(6, 23))
        returned = self.store.create_event(title='青岛返南京', location='南京南站',
            notes='次日早晨抵达南京。', starts_at=at(7, 7), ends_at=at(7, 9))
        maps, prompts = Mock(), []
        maps.discover.return_value = dict(origin=None, nearby=[])
        maps.enrich.return_value = None

        async def reply(raw, **kwargs):
            prompt = json.loads(raw)
            prompts.append(prompt)
            if prompt['stage'] == 'resolve_location':
                maps.discover.assert_not_called()
                self.assertEqual({e['id'] for e in prompt['calendar_timeline']}, {trip['id'], returned['id']})
                work = next(o for o in prompt['origin_options'] if o['ref'] == 'fact:work')
                self.assertEqual(work['field'], 'workplace_location')
                origin = dict(ref=f"event:{returned['id']}:{returned['revision']}",
                    name='南京南站', address='', coordinates=None, conditional=True)
                return SimpleNamespace(status='completed', final_message=json.dumps(
                    dict(summary='较晚的返程修正此前住宿线索', origin=origin)))
            self.assertEqual(prompt['origin_options'][0]['name'], '南京南站')
            origin = {k: prompt['origin_options'][0][k] for k in ('ref', 'name', 'address', 'coordinates')}
            return SimpleNamespace(status='completed', final_message=json.dumps(dict(
                summary='本次没有合适展览', origin=origin, candidates=[])))

        runtime = SimpleNamespace(create_thread=AsyncMock(), close=AsyncMock(),
                                  run_turn=AsyncMock(side_effect=reply))
        worker = ProactiveWorker(self.store, maps=maps, runtime_factory=lambda: runtime)
        worker.context.snapshot = Mock(return_value=self.context)
        context, _, prompt, triggers = worker.prepare(at(7, 9))
        trigger = next(t for t in triggers if t['target_date'] == '2026-11-07')
        output = asyncio.run(worker.research(prompt, trigger, context, Event(), at(7, 9)))
        self.assertFalse(output['items'])
        self.assertEqual(prompts[0]['stage'], 'resolve_location')
        self.assertTrue(all(call.args[0][0]['name'] == '南京南站' for call in maps.discover.call_args_list))

    def test_trip_alone_enables_next_day_without_assuming_its_calendar_end_is_checkout(self):
        self.store.create_event(title='成都住宿：城南旅馆', notes='前晚出发，返程待定',
            location='西安集合点', starts_at=at(6, 21), ends_at=at(6, 23))
        worker = ProactiveWorker(self.store, maps=Mock())
        worker.context.snapshot = Mock(return_value=dict(self.context, historical_locations=[]))
        context, _, prompt, triggers = worker.prepare(at(7, 9))
        trigger = next(t for t in triggers if t['target_date'] == '2026-11-07')
        options = origin_options(trigger, context, prompt['calendar_timeline'])
        self.assertEqual(options[0]['title'], '成都住宿：城南旅馆')
        self.assertEqual(options[0]['name'], '西安集合点')  # Still source data, not a presumed destination.
        self.assertEqual(options[0]['ends_at'], at(6, 23))

    def test_unknown_target_location_stops_search_instead_of_falling_back_to_work(self):
        runtime = SimpleNamespace(create_thread=AsyncMock(), close=AsyncMock(),
            run_turn=AsyncMock(return_value=SimpleNamespace(status='completed',
                final_message=json.dumps(dict(summary='只有工作地点，目标空档去向未知', origin=None)))))
        maps = Mock()
        worker = ProactiveWorker(self.store, maps=maps, runtime_factory=lambda: runtime)
        output = asyncio.run(worker.research(dict(timezone='Asia/Shanghai'),
            dict(kind='free_prepare', target_date='2026-11-07'), self.context, Event(), at(6, 22)))
        self.assertEqual(output['items'], [])
        maps.discover.assert_not_called()
        maps.enrich.assert_not_called()
        self.assertEqual(runtime.run_turn.await_count, 1)

    def test_exhibitions_require_the_same_location_basis_and_keep_stay_conditional(self):
        candidate = dict(category='exhibition', title='专题展', body='展期内可参观', reason='有特色的展品',
            url='https://museum.example/exhibition', profile_ids=[], dh_fact_ids=[], restaurant=None)
        origin = dict(ref='event:1:1', name='目的地旅馆', address='', coordinates=None)
        payload = dict(origin=origin, candidates=[candidate])
        options = [dict(origin, conditional=True, basis='住宿日程')]
        selected = select_candidates(payload, options, target_date='2026-11-07',
            timezone='Asia/Shanghai', now=at(6, 22))
        self.assertIn('如果在目的地旅馆附近', selected['items'][0]['body'])
        self.assertTrue(selected['items'][0]['discovery']['origin']['conditional'])
        self.assertEqual(select_candidates(payload, [], target_date='2026-11-07',
            timezone='Asia/Shanghai', now=at(6, 22))['items'], [])


if __name__ == '__main__':
    unittest.main()
