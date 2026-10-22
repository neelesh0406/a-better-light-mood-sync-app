import io
import json
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest.mock import MagicMock, patch

from app import Controller
from live_sync import ColorMapper, LiveSync, settings, HELPER


class MappingTests(unittest.TestCase):
    def video(self, rgb, luma=.5):
        return {'average': rgb, 'vibrant': rgb, 'luma': luma, 'time': time.time()}

    def test_single_source_only_and_bounded_rate(self):
        for invalid in ({'mode':'hybrid'}, {'fps':100}, {'smoothing':100}, {'follow':1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                settings(invalid)

    def test_screen_no_smoothing_preserves_hard_cut(self):
        mapper = ColorMapper(settings({'boost':100}))
        red = mapper.map(self.video([255,0,0]), None, time.time())
        blue = mapper.map(self.video([0,0,255]), None, time.time())
        self.assertEqual([red[k] for k in ('r','g','b')], [255,0,0])
        self.assertEqual([blue[k] for k in ('r','g','b')], [0,0,255])

    def test_black_scene_dims_without_turning_off_or_dividing_by_zero(self):
        mapper = ColorMapper(settings({}))
        mapper.map(self.video([0,0,255]), None, time.time())
        black = mapper.map(self.video([0,0,0],0), None, time.time())
        self.assertTrue(black['state'])
        self.assertEqual(black['dimming'],10)
        self.assertEqual(black['b'],255)

    def test_follow_brightness_honors_maximum(self):
        mapper = ColorMapper(settings({'follow':True,'brightness':60}))
        bright = mapper.map(self.video([255,255,255],1),None,time.time())
        dark = mapper.map(self.video([50,50,50],.1),None,time.time())
        self.assertEqual(bright['dimming'],60)
        self.assertLess(dark['dimming'],bright['dimming'])

    def test_audio_has_no_video_dependency_and_silence_decays(self):
        mapper = ColorMapper(settings({'mode':'music'}))
        now = time.time()
        audio = {'time':now,'rms':.2,'bass':.2,'mid':.02,'treble':.01}
        loud = mapper.map(None,audio,now)
        stale = mapper.map(None,audio,now+1)
        self.assertGreater(loud['dimming'],stale['dimming'])
        self.assertEqual(stale['dimming'],10)

    def test_frequency_balance_changes_music_color(self):
        mapper = ColorMapper(settings({'mode':'music'}))
        now = time.time()
        low = mapper.map(None,{'time':now,'rms':.1,'bass':1,'mid':0,'treble':0},now)
        high = mapper.map(None,{'time':now,'rms':.1,'bass':0,'mid':0,'treble':1},now)
        self.assertNotEqual([low[k] for k in ('r','g','b')],[high[k] for k in ('r','g','b')])


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.c = Controller(Path(self.temp.name)/'state.json')
        self.sync = self.c.sync

    def tearDown(self):
        self.temp.cleanup()

    def test_reader_keeps_only_latest_sample(self):
        process = MagicMock()
        process.stdout = io.StringIO(''.join(json.dumps({'event':'video','average':[i,0,0]})+'\n' for i in range(100)))
        self.sync.process = process
        self.sync.read(process)
        self.assertEqual(self.sync.video['average'],[99,0,0])
        self.assertIsNone(self.sync.audio)

    def test_missing_permission_never_launches_capture(self):
        with patch('live_sync.capture_info',return_value={'permission':False}), patch('live_sync.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError,'permission'):
                self.sync.start('192.168.1.8',{})
            popen.assert_not_called()

    def test_stream_does_not_wait_for_round_trip_and_restores(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        responses=[]
        sent=[]
        def send(data):
            sent.append(json.loads(data))
            responses.append(b'{"method":"setPilot","result":{"success":true}}')
            if len(sent)==3:self.sync.stop_event.set()
            return len(data)
        def recv(_):
            if responses:return responses.pop(0)
            raise BlockingIOError()
        fake.send.side_effect=send
        fake.recv.side_effect=recv
        process=MagicMock()
        process.poll.return_value=None
        self.sync.video={'time':time.time()}
        self.sync.original={'state':True,'dimming':80,'sceneId':0,'r':255,'g':0,'b':0}
        samples=[{'state':True,'r':i,'g':0,'b':255,'dimming':70} for i in range(3)]
        with patch('live_sync.socket.socket',return_value=fake), patch('live_sync.ColorMapper') as mapper, patch.object(self.sync,'terminate'), patch('wiz.request',return_value={'result':{'success':True}}) as restore:
            mapper.return_value.map.side_effect=samples
            self.sync.run('192.168.1.8',settings({}),process)
        self.assertEqual(len(sent),3)
        self.assertEqual(self.sync.public()['acknowledged'],3)
        fake.setblocking.assert_called_once_with(False)
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args[2]['r'],255)
        self.assertFalse(self.sync.public()['active'])

    def test_manual_command_stops_sync_without_restoring_over_new_command(self):
        self.sync=MagicMock()
        self.c.sync=self.sync
        with patch('wiz.request',side_effect=[{'result':{'state':True,'dimming':80}},
                                              {'result':{'success':True}},
                                              {'result':{'state':False,'dimming':80}}]):
            self.c.apply('192.168.1.8',{'state':False})
        self.sync.stop.assert_called_once_with(restore=False,ip='192.168.1.8')


@unittest.skipUnless(HELPER.exists(), 'Build the macOS helper to test native sampling.')
class NativeSamplerTests(unittest.TestCase):
    def test_synthetic_image_and_audio_without_capture_access(self):
        result = subprocess.run([str(HELPER),'--self-test'],capture_output=True,text=True,check=True,timeout=5)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        video = next(event for event in events if event['event']=='video')
        audio = next(event for event in events if event['event']=='audio')
        self.assertEqual(video['vibrant'],[255,0,0])
        self.assertEqual(video['average'],[255,0,0])
        self.assertAlmostEqual(audio['rms'],.14142,places=4)
        self.assertGreater(audio['bass'],audio['treble'])


if __name__ == '__main__':
    unittest.main()
