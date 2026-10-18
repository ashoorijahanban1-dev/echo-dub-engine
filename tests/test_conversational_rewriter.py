import pytest
from pathlib import Path
import config
from workers.dubbing import (
    PersianConversationalRewriter,
    LinguisticQC,
    TechnicalGlossary,
    PersianDubbingWorker,
    ProvenanceViolationException,
    SynthesisException
)

def test_conversational_rewriter_transformations():
    rewriter = PersianConversationalRewriter()
    text = "این یک کانتینر داکر می‌باشد که به منظور دیپلویمنت ایجاد گردید و لازم به ذکر است که پایپ‌لاین اجرا خواهد شد."
    rewritten = rewriter._rewrite_single_segment(text, source_en="docker container deployment pipeline", duration=10.0)
    
    # Assert bookish verb endings replaced with conversational ones
    assert "می‌باشد" not in rewritten
    assert "هست" in rewritten
    assert "گردید" not in rewritten
    assert "شد" in rewritten
    assert "به منظور" not in rewritten
    assert "برای اینکه" in rewritten
    assert "لازم به ذکر است که" not in rewritten
    assert "نکته اینجاست که" in rewritten
    # Assert technical keywords preserved
    assert "کانتینر" in rewritten
    assert "داکر" in rewritten
    assert "دیپلویمنت" in rewritten
    assert "پایپ‌لاین" in rewritten

def test_linguistic_qc_stiff_formal_detection():
    # Should flag stiff/bookish phrase in conversational mode
    ok, failures = LinguisticQC.validate_segment(
        text_fa="لازم به ذکر است که سیستم آماده است.",
        duration=5.0,
        source_en="Note that system is ready.",
        style="conversational_professional"
    )
    assert not ok
    assert any("Stiff/bookish phrase detected" in f for f in failures)

def test_linguistic_qc_excessive_slang_detection():
    # Should flag excessive street slang
    ok, failures = LinguisticQC.validate_segment(
        text_fa="سلام داداش، این کانتینر خیلی خفن کار می‌کنه!",
        duration=5.0,
        source_en="Hello, this container works well.",
        style="conversational_professional"
    )
    assert not ok
    assert any("Inappropriate street slang detected" in f for f in failures)

def test_linguistic_qc_sentence_length():
    # Extremely long unbroken sentence should trigger length limit
    long_sentence = "این " + "خیلی " * 30 + "خوب است."
    ok, failures = LinguisticQC.validate_segment(
        text_fa=long_sentence,
        duration=10.0,
        source_en="This is very good.",
        style="conversational_professional"
    )
    assert not ok
    assert any("Sentence exceeds max word limit" in f for f in failures)

def test_linguistic_qc_technical_preservation():
    # English mentions 'kubernetes' and 'docker' but Persian missing one
    ok, failures = LinguisticQC.validate_segment(
        text_fa="ما از داکر استفاده می‌کنیم.",
        duration=5.0,
        source_en="We use kubernetes and docker.",
        style="conversational_professional"
    )
    assert not ok
    assert any("Technical term 'kubernetes' was not preserved" in f for f in failures)

def test_cache_key_isolation_by_style(tmp_path):
    worker_conv = PersianDubbingWorker(gemini_api_key="mock_key")
    worker_conv.style = "conversational_professional"
    worker_conv.style_version = "fa_conversational_v1"
    worker_conv.cache_dir = tmp_path

    worker_formal = PersianDubbingWorker(gemini_api_key="mock_key")
    worker_formal.style = "formal"
    worker_formal.style_version = "fa_formal_v0"
    worker_formal.cache_dir = tmp_path

    text = "سلام دوستان، در این درس با داکر آشنا می‌شیم."
    out_conv = tmp_path / "conv.wav"
    out_formal = tmp_path / "formal.wav"

    worker_conv.synthesize_voice(text, out_conv, voice_name="Puck")
    worker_formal.synthesize_voice(text, out_formal, voice_name="Puck")

    # Cache directory should contain two distinct cached files due to different style_version in hash
    cache_files = list(tmp_path.glob("tts_*.wav"))
    assert len(cache_files) == 2


def test_provenance_violation_on_formal_audio_reuse(tmp_path):
    worker = PersianDubbingWorker(gemini_api_key="mock_key")
    worker.style = "conversational_professional"
    worker.style_version = "fa_conversational_v1"

    formal_file = tmp_path / "formal_seg.wav"
    formal_file.write_bytes(b"RIFF" + b"\x00" * 100)
    import hashlib
    formal_hash = hashlib.sha256(formal_file.read_bytes()).hexdigest()
    prohibited = {formal_hash}

    tainted_segment = {
        "id": "seg_12",
        "audio_path": str(formal_file),
        "style_version": "fa_conversational_v1"
    }

    with pytest.raises(ProvenanceViolationException) as exc_info:
        worker.verify_segment_provenance(tainted_segment, prohibited_hashes=prohibited)
    assert "PROVENANCE_VIOLATION" in str(exc_info.value)


def test_provenance_verification_passes_for_genuine_segment(tmp_path):
    worker = PersianDubbingWorker(gemini_api_key="mock_key")
    worker.style = "conversational_professional"
    worker.style_version = "fa_conversational_v1"

    genuine_file = tmp_path / "genuine_seg.wav"
    genuine_file.write_bytes(b"RIFF" + b"\x01" * 100)

    segment = {
        "id": "seg_1",
        "audio_path": str(genuine_file),
        "style_version": "fa_conversational_v1"
    }

    hash_val = worker.verify_segment_provenance(segment, prohibited_hashes=set())
    assert segment["provenance"] == "gemini_generated"
    assert segment["sha256"] == hash_val


def test_quota_429_enters_waiting_retry_without_fallback(monkeypatch, tmp_path):
    import urllib.error
    worker = PersianDubbingWorker(gemini_api_key="valid_real_key_not_mock")
    worker.tts_engine = "google_gemini"
    worker.cache_dir = tmp_path

    def mock_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.test", 429, "Rate limit exceeded (10 RPM)", {}, None)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    out_wav = tmp_path / "out.wav"
    with pytest.raises(SynthesisException) as exc_info:
        worker.synthesize_voice("متن تستی", out_wav, voice_name="Puck")

    assert "HTTP 429" in str(exc_info.value)
    assert not out_wav.exists()


def test_piper_adapter_initialization():
    from workers.dubbing import PiperTTSAdapter
    adapter = PiperTTSAdapter(model_path="test.onnx", config_path="test.json")
    assert adapter.model_path.name == "test.onnx"
    assert adapter.config_path.name == "test.json"


def test_self_hosted_piper_synthesis_flow(monkeypatch, tmp_path):
    worker = PersianDubbingWorker(gemini_api_key="mock_key")
    worker.cache_dir = tmp_path
    worker.tts_engine = "self_hosted_piper"

    fake_wav = tmp_path / "fake.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 100)

    def mock_synth(text, out_path):
        out_path.write_bytes(fake_wav.read_bytes())
        return out_path

    monkeypatch.setattr(worker.piper_adapter, "synthesize", mock_synth)

    dest_wav = tmp_path / "dest.wav"
    out_path, cost = worker.synthesize_voice("سلام این تست پیپر است.", dest_wav)
    assert out_path.exists()
    assert cost == 0.0 # 100% Free!


def test_tehrani_genz_transformations():
    rewriter = PersianConversationalRewriter()
    text = "من عمار هستم و توی دانشگاه تدریس می‌کنم. شما می‌توانید این‌ها را ببینید و موفق باشید."
    rewritten = rewriter._rewrite_single_segment(text, source_en="", duration=5.0)
    assert "تو" in rewritten
    assert "درس میدم" in rewritten
    assert "می‌تونین" in rewritten
    assert "اینا" in rewritten
    assert "ببینین" in rewritten
    assert "موفق باشین" in rewritten


def test_edge_tts_adapter_initialization():
    from workers.dubbing import EdgeTTSAdapter
    adapter = EdgeTTSAdapter(voice="fa-IR-FaridNeural", rate="+12%", pitch="+3Hz")
    assert adapter.voice == "fa-IR-FaridNeural"
    assert adapter.rate == "+12%"
    assert adapter.pitch == "+3Hz"


def test_edge_tts_synthesis_flow(monkeypatch, tmp_path):
    worker = PersianDubbingWorker(gemini_api_key="mock_key")
    worker.cache_dir = tmp_path
    worker.tts_engine = "edge_tts_farid"

    fake_wav = tmp_path / "fake.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 100)

    def mock_synth(text, out_path):
        out_path.write_bytes(fake_wav.read_bytes())
        return out_path

    monkeypatch.setattr(worker.edge_adapter, "synthesize", mock_synth)

    dest_wav = tmp_path / "dest.wav"
    out_path, cost = worker.synthesize_voice("سلام بچه‌ها این تست فرید است.", dest_wav)
    assert out_path.exists()
    assert cost == 0.0 # 100% Free!


def test_mux_solo_audio_command(monkeypatch, tmp_path):
    worker = PersianDubbingWorker(gemini_api_key="mock_key")
    executed_cmds = []

    def mock_run(cmd, *args, **kwargs):
        executed_cmds.append(cmd)
        class MockProc:
            returncode = 0
            stderr = ""
        return MockProc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/ffmpeg")

    v_path = tmp_path / "video.mp4"
    a_path = tmp_path / "audio.wav"
    out_path = tmp_path / "out.mp4"

    worker.mux_audio(v_path, a_path, out_path, mode="solo_persian")
    assert len(executed_cmds) == 1
    cmd = executed_cmds[0]
    # Verify ONLY Persian audio track mapped (0:v:0 and 1:a:0, no 0:a)
    assert "-map" in cmd
    assert "1:a:0" in cmd
    assert "0:a:0?" not in cmd


def test_audio_mastering_studio(monkeypatch, tmp_path):
    from workers.dubbing import AudioMasteringStudio
    fake_in = tmp_path / "in.mp3"
    fake_in.write_bytes(b"FAKE_AUDIO_DATA")
    fake_out = tmp_path / "out.wav"

    executed_cmds = []
    def mock_run(cmd, *args, **kwargs):
        executed_cmds.append(cmd)
        fake_out.write_bytes(b"RIFF" + b"\x00" * 100)
        class MockProc:
            returncode = 0
            stderr = ""
        return MockProc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/ffmpeg")

    res = AudioMasteringStudio.master_audio(fake_in, fake_out)
    assert res == fake_out
    assert fake_out.exists()
    assert len(executed_cmds) == 1
    cmd = executed_cmds[0]
    assert "-af" in cmd
    assert "loudnorm" in cmd[cmd.index("-af") + 1]
    assert "equalizer" in cmd[cmd.index("-af") + 1]


def test_group_segments_into_sentences_merges_fragments():
    raw_subtitles = [
        {"id": 1, "start": 0.0, "end": 2.0, "text": "In this technical lesson,"},
        {"id": 2, "start": 2.1, "end": 4.5, "text": "we are going to learn"},
        {"id": 3, "start": 4.6, "end": 7.0, "text": "how Docker containers operate."},
        {"id": 4, "start": 7.2, "end": 10.0, "text": "Let's explore the architecture!"},
        {"id": 5, "start": 12.5, "end": 15.0, "text": "Notice the pause before this section"}
    ]

    grouped = PersianDubbingWorker.group_segments_into_sentences(raw_subtitles, max_duration=14.0, max_gap=1.0)

    # Segments 1, 2, 3 should merge into one continuous sentence because there's no terminal punctuation
    assert len(grouped) == 3

    # Group 1: 0.0 to 7.0
    g1 = grouped[0]
    assert g1["id"] == 1
    assert g1["start"] == 0.0
    assert g1["end"] == 7.0
    assert g1["text"] == "In this technical lesson, we are going to learn how Docker containers operate."
    assert g1["sub_segments_count"] == 3

    # Group 2: 7.2 to 10.0 (ends with !)
    g2 = grouped[1]
    assert g2["id"] == 2
    assert g2["start"] == 7.2
    assert g2["end"] == 10.0
    assert g2["text"] == "Let's explore the architecture!"
    assert g2["sub_segments_count"] == 1

    # Group 3: 12.5 to 15.0 (separated by 2.5s pause)
    g3 = grouped[2]
    assert g3["id"] == 3
    assert g3["start"] == 12.5
    assert g3["end"] == 15.0
    assert g3["text"] == "Notice the pause before this section"


def test_extended_vocalization_and_diacritics():
    rewriter = PersianConversationalRewriter()
    text = "به عنوان توسعه‌دهنده باید پیکربندی و مدیریت سیستم و فرآیند عملکرد آن را دقیقا بدانیم."
    rewritten = rewriter._rewrite_single_segment(text, source_en="", duration=8.0)

    assert "تَوسِعه‌دَهَنده" in rewritten
    assert "پِیکَربَندی" in rewritten
    assert "مُدیریَّت" in rewritten
    assert "فَرایند" in rewritten
    assert "عَمَلکَرد" in rewritten
    assert "دَقیقاً" in rewritten






