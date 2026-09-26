"""ffmpeg lega RTP e RTCP (porta+1) per ogni m= dell'SDP: nessuna sovrapposizione."""
from custom_components.vimar_intercom import const as C


def test_ffmpeg_rtp_rtcp_ports_do_not_overlap():
    ports = [C.FFMPEG_VIDEO_PORT, C.FFMPEG_AV_VIDEO_PORT, C.FFMPEG_AV_AUDIO_PORT]
    used = [p + d for p in ports for d in (0, 1)]
    assert len(used) == len(set(used))
