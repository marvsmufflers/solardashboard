import gzip

from solardash.storage.importer import run_import
from solardash.storage.sqlite import HistoryStore

HEAD = "time_utc,time_local,pv_w,load_w,battery_w,soc,outside_temp_c,chargeverter_w_inferred,data_present\n"


def _write(folder, rows, gz=True):
    folder.mkdir(parents=True, exist_ok=True)
    text = HEAD + "".join(rows)
    if gz:
        with gzip.open(folder / "cabin_1min.csv.gz", "wt") as f:
            f.write(text)
    else:
        (folder / "cabin_1min.csv").write_text(text)


def test_import_only_before_own_data_and_once(tmp_path):
    base = 1780000000 - 1780000000 % 3600
    rows = []
    for i in range(240):  # 4 hours of minutes
        ts = base + i * 60
        iso = __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").timezone.utc).isoformat()
        rows.append(f"{iso},x,{1000 + i},{300},{500},{50 + i / 10},{5.0},{0},1\n")
    rows.append(f"{rows[-1].split(',')[0][:-6]}+00:00,x,,,,,,,0\n")  # a missing minute
    _write(tmp_path / "import", rows)
    st = HistoryStore(tmp_path / "s.db", retention_days=0)
    own = base + 120 * 60  # the app's own first reading two hours in
    st.insert(own, {"total/battery_state_of_charge": 99.0})
    r = run_import(st, str(tmp_path / "s.db"))
    assert not r["errors"] and r["imported"][0]["minutes"] == 120
    assert st.query("total/battery_state_of_charge", own, own) == [(own, 99.0)]  # never overwritten
    assert st.query("total/pv_power", base, base) == [(base, 1000.0)]
    assert st.query("weather/temperature", base, base) == [(base, 5.0)]
    assert len(st.query("total/pv_power", base, own - 1, step=3600)) == 2  # hourly rollups exist
    assert (tmp_path / "import" / "cabin_1min.csv.gz.imported").exists()
    assert run_import(st, str(tmp_path / "s.db"))["imported"] == []  # not read twice


def test_import_rejects_wrong_file(tmp_path):
    (tmp_path / "import").mkdir()
    (tmp_path / "import" / "other.csv").write_text("a,b\n1,2\n")
    st = HistoryStore(tmp_path / "s.db")
    r = run_import(st, str(tmp_path / "s.db"))
    assert r["imported"] == [] and "time_utc" in r["errors"][0]
