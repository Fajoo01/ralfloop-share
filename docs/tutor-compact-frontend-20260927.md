# Tutor compact frontend

Study content remains central. Desktop navigation uses a narrow sidebar; mobile
navigation scrolls horizontally. The Tutor portrait is 54 px on desktop and 48 px
inside mobile lessons. It replaces the previous large image column. Feedback no
longer duplicates the portrait. Existing reader settings and reduced motion remain.

The original SVG portrait keeps Bot-tazzi's hat, moustache and red scarf. Only its
mouth animates. Audio playback uses the existing RMS analyser; browser TTS keeps
the existing speech-event animation. Pause, completion and errors reset it.

Layout reference: https://github.com/open-webui/open-webui (conversation centered
workspace and compact navigation). No external code, fonts or image assets copied.

The live Teacher broker had five optional media tools, while this client expected
an exact match of the original thirteen. The client now accepts those five known
optional names while still rejecting unknown tools and missing core capabilities.
The optional tools remain unavailable to this client's call API.

Validation: 66 web/avatar tests; 4 deployment tests; real Chrome at 1365 and 390 px,
login, lesson navigation, mouth start/reset, reduced motion, no horizontal overflow
or JavaScript exceptions. Live broker health and public page/assets return success.

Previous web release: d9a7139ed47ac528b21317ab212c313efa3b4df6.
Deployment uses tools/install_teacher_web.py and its manifest/rollback workflow.
