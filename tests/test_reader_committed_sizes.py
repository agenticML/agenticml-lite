"""Committed sizes are looked up once per public reader call, never across calls."""
import json
import struct

from agenticml.store.reader import RunReader

POINTS = 5000


def _reader(tmp_path):
    run = tmp_path / 'runs' / 'run-1'
    (run / 'scalars').mkdir(parents=True)
    (run / 'meta.json').write_text(json.dumps({
        'id': 'run-1', 'name': 'r', 'status': 'running', 'created_at': '2026-01-01T00:00:00Z'}))
    data = b''.join(struct.pack('<If', step, float(step)) for step in range(POINTS))
    (run / 'scalars' / '0.bin').write_bytes(data)
    (run / 'scalars' / 'index.json').write_text(json.dumps({'version': 1, 'series': {'loss': {'id': 0}}}))
    calls = []

    def committed(run_id, rel):
        calls.append(rel)
        return len(data)

    return RunReader(tmp_path, 'run-1', committed_size=committed), calls


def test_series_asks_for_the_committed_size_once_per_call(tmp_path):
    reader, calls = _reader(tmp_path)
    # More points than max_points and no overview: _series_locked and
    # _incremental both need the size of the same file.
    result = reader.series('loss', max_points=100)
    assert result['mode'] == 'overview' and result['total'] == POINTS
    assert len(calls) == 1
    # The memo does not outlive the call.
    reader.series('loss', max_points=100)
    assert len(calls) == 2


def test_other_calls_use_one_lookup_each_and_never_keep_it(tmp_path):
    reader, calls = _reader(tmp_path)
    assert reader.tail('loss')['count'] == POINTS
    assert reader.read_records('loss', 0, 3)[0][0][0] == 0
    assert reader.record_sizes() == {'loss': POINTS * 8}
    assert len(calls) == 3
    assert reader.record_sizes() == {'loss': POINTS * 8}
    assert len(calls) == 4


def test_memo_is_per_call_and_sees_a_new_size_on_the_next_call(tmp_path):
    reader, _ = _reader(tmp_path)
    size = [8]
    reader._committed_size = lambda run_id, rel: size[0]
    assert reader.series('loss', max_points=100)['total'] == 1
    size[0] = 16
    assert reader.series('loss', max_points=100)['total'] == 2


def test_without_a_provider_nothing_changes(tmp_path):
    _, calls = _reader(tmp_path)
    plain = RunReader(tmp_path, 'run-1')
    assert plain.series('loss', max_points=100)['total'] == POINTS
    assert plain.record_sizes() == {'loss': POINTS * 8}
    assert calls == []
