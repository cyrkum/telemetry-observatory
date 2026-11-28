from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api import main


def test_gpu_sample_is_validated_and_stored(monkeypatch, tmp_path):
    monkeypatch.setenv('ADMIN_TOKEN', 'a' * 32)
    monkeypatch.setattr(main, 'audit_path', tmp_path / 'api.jsonl')
    monkeypatch.setattr(main, 'current_policy', lambda: {'retention_days': 7})
    stored = []

    class Samples:
        def insert_one(self, item):
            stored.append(item)

        def find(self, query, projection):
            return self

        def sort(self, key, direction):
            return self

        def limit(self, count):
            return stored[:count]

    monkeypatch.setattr(main, 'samples', Samples())
    data = {'host': 'mac', 'sampled_at': datetime.now(timezone.utc), 'cpu_percent': 10, 'gpu_percent': 37,
            'memory': {}, 'swap': {}, 'disks': [], 'disk_io': {}, 'network_io': {}, 'applications': []}
    main.post_sample(main.Sample(**data), 'Bearer ' + 'a' * 32)
    assert stored[0]['gpu_percent'] == 37
    assert main.get_samples(1)[0]['gpu_percent'] == 37
    data.pop('gpu_percent')
    assert main.Sample(**data).gpu_percent is None
    for invalid in (-1, 101):
        with pytest.raises(ValidationError):
            main.Sample(**data, gpu_percent=invalid)
    ai = {'ollama': {'reachable': True, 'version': '0.40.19', 'models': []}, 'processes': [], 'tokens': None}
    main.post_sample(main.Sample(**data, local_ai=ai), 'Bearer ' + 'a' * 32)
    assert stored[-1]['local_ai'] == ai
    assert stored[0]['local_ai'] is None


def test_admin_rejects_invalid_token(monkeypatch, tmp_path):
    monkeypatch.setenv('ADMIN_TOKEN', 'a' * 32)
    monkeypatch.setattr(main, 'audit_path', tmp_path / 'api.jsonl')
    with pytest.raises(HTTPException) as error:
        main.require_admin('Bearer wrong')
    assert error.value.status_code == 401
    assert 'denied' in (tmp_path / 'api.jsonl').read_text()


def test_retention_update_recalculates_existing_expiry(monkeypatch, tmp_path):
    monkeypatch.setenv('ADMIN_TOKEN', 'a' * 32)
    monkeypatch.setattr(main, 'audit_path', tmp_path / 'api.jsonl')
    at = datetime(2026, 1, 1)

    class Settings:
        def find_one(self, query):
            return {'frequency_seconds': 3, 'retention_days': 2}

        def update_one(self, query, change, upsert=False):
            assert change['$set']['retention_days'] == 2
            assert upsert

    class Samples:
        expiry = None

        def find(self, query, fields):
            return [{'_id': 1, 'sampled_at': at}]

        def update_one(self, query, change):
            self.expiry = change['$set']['expires_at']

    fake = Samples()
    monkeypatch.setattr(main, 'settings', Settings())
    monkeypatch.setattr(main, 'samples', fake)
    result = main.put_settings({'frequency_seconds': 3, 'retention_days': 2}, 'Bearer ' + 'a' * 32)
    assert result['frequency_seconds'] == 3
    assert fake.expiry == datetime(2026, 1, 3, tzinfo=timezone.utc)


def test_history_bucket_keeps_point_count_bounded():
    assert main.history_bucket_seconds(1) == 10
    assert main.history_bucket_seconds(24) == 240
    assert main.history_bucket_seconds(168) == 1680
    for hours in (1, 24, 168, 720):
        assert hours * 3600 / main.history_bucket_seconds(hours) <= main.HISTORY_MAX_POINTS


def test_history_returns_bucket_averages_newest_first(monkeypatch):
    class Samples:
        pipeline = None

        def aggregate(self, pipeline):
            self.pipeline = pipeline
            return [
                {'_id': datetime(2026, 1, 1, 12, 4), 'cpu_percent': 12.345, 'gpu_percent': 37.46, 'memory_percent': 55.55, 'samples': 24},
                {'_id': datetime(2026, 1, 1, 12, 0), 'cpu_percent': None, 'gpu_percent': None, 'memory_percent': 50.0, 'samples': 1},
                {'_id': datetime(2026, 1, 1, 11, 56), 'cpu_percent': 0, 'gpu_percent': 0, 'memory_percent': 50.0, 'samples': 1},
            ]

    fake = Samples()
    monkeypatch.setattr(main, 'samples', fake)
    result = main.get_history(24)
    group = fake.pipeline[1]['$group']
    assert group['_id']['$dateTrunc']['binSize'] == 240
    assert group['gpu_percent'] == {'$avg': '$gpu_percent'}
    assert fake.pipeline[-1] == {'$sort': {'_id': main.DESCENDING}}
    assert result[0] == {'sampled_at': '2026-01-01T12:04:00+00:00', 'cpu_percent': 12.3, 'gpu_percent': 37.5, 'memory': {'percent': 55.5}, 'samples': 24}
    assert result[1]['cpu_percent'] == 0
    assert result[1]['gpu_percent'] is None
    assert result[2]['gpu_percent'] == 0
