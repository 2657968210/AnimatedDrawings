# Web service for AnimatedDrawings — upload a drawing, download an animation
import os
import uuid
import shutil
import threading
import time
import logging
import yaml
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory, url_for

# ── paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.resolve()          # /workspaces/AnimatedDrawings
UPLOAD_DIR = Path(__file__).parent / 'static' / 'uploads'
OUTPUT_DIR = Path(__file__).parent / 'static' / 'outputs'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Keep outputs for 30 minutes then auto-delete
OUTPUT_TTL = 1800

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024   # 16 MB upload limit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── motion → (bvh_prefix, retarget_cfg) ─────────────────────────────────────
MOTIONS = {
    'dab':            {'label': '🕺 Dab',           'retarget': 'fair1_ppf'},
    'zombie':         {'label': '🧟 Zombie Walk',    'retarget': 'fair1_ppf'},
    'jumping':        {'label': '🦘 Jumping',        'retarget': 'fair1_ppf'},
    'wave_hello':     {'label': '👋 Wave Hello',     'retarget': 'fair1_ppf'},
    'jumping_jacks':  {'label': '⭐ Jumping Jacks',  'retarget': 'cmu1_pfp'},
    'jesse_dance':    {'label': '💃 Jesse Dance',    'retarget': 'mixamo_fff'},
}

PRESET_CHARS = {str(i): f'char{i}' for i in range(1, 7)}

# ── helper: build mvc yaml and render ────────────────────────────────────────
def render_animation(char_cfg_path: str, motion_key: str, output_path: str) -> None:
    """Render a GIF using Mesa headless rendering."""
    motion = MOTIONS[motion_key]
    retarget_cfg_fn = str(ROOT / 'examples' / 'config' / 'retarget' / f"{motion['retarget']}.yaml")
    motion_cfg_fn   = str(ROOT / 'examples' / 'config' / 'motion' / f'{motion_key}.yaml')

    mvc_cfg = {
        'scene': {
            'ANIMATED_CHARACTERS': [{
                'character_cfg': char_cfg_path,
                'motion_cfg':    motion_cfg_fn,
                'retarget_cfg':  retarget_cfg_fn,
            }]
        },
        'view': {'USE_MESA': True},
        'controller': {
            'MODE': 'video_render',
            'OUTPUT_VIDEO_PATH': output_path,
        }
    }

    tmp_cfg = str(Path(output_path).parent / 'mvc_tmp.yaml')
    with open(tmp_cfg, 'w') as f:
        yaml.dump(mvc_cfg, f)

    # Must import here (after os.environ is set in mesa_view) 
    import animated_drawings.render as ad_render
    ad_render.start(tmp_cfg)
    os.remove(tmp_cfg)


def _cleanup_old_files():
    """Background thread: remove output files older than OUTPUT_TTL seconds."""
    while True:
        time.sleep(300)
        now = time.time()
        for f in list(OUTPUT_DIR.iterdir()) + list(UPLOAD_DIR.iterdir()):
            try:
                if f.is_file() and now - f.stat().st_mtime > OUTPUT_TTL:
                    f.unlink()
            except Exception:
                pass


threading.Thread(target=_cleanup_old_files, daemon=True).start()

# ── routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    chars = [{'id': str(i), 'name': f'角色 {i}', 'img': f'char{i}_texture.png'}
             for i in range(1, 7)]
    motions = [{'key': k, 'label': v['label']} for k, v in MOTIONS.items()]
    return render_template('index.html', chars=chars, motions=motions)


@app.route('/generate', methods=['POST'])
def generate():
    """Generate animation from a preset character or uploaded image."""
    motion_key = request.form.get('motion', 'dab')
    if motion_key not in MOTIONS:
        return jsonify({'error': '无效的动作'}), 400

    job_id = uuid.uuid4().hex
    output_gif = str(OUTPUT_DIR / f'{job_id}.gif')

    uploaded_file = request.files.get('image')

    if uploaded_file and uploaded_file.filename:
        # ── custom upload path ─────────────────────────────────────────────
        ext = Path(uploaded_file.filename).suffix.lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
            return jsonify({'error': '只支持 PNG / JPG / WEBP 格式'}), 400

        char_dir = UPLOAD_DIR / job_id
        char_dir.mkdir(parents=True, exist_ok=True)
        img_path = str(char_dir / f'image{ext}')
        uploaded_file.save(img_path)

        # Try to auto-annotate via torchserve (optional)
        try:
            import sys
            sys.path.insert(0, str(ROOT / 'examples'))
            from image_to_annotations import image_to_annotations
            image_to_annotations(img_path, str(char_dir))
            char_cfg_path = str(char_dir / 'char_cfg.yaml')
        except Exception as e:
            logger.warning(f'Auto-annotation failed (torchserve not running?): {e}')
            shutil.rmtree(str(char_dir), ignore_errors=True)
            return jsonify({
                'error': (
                    '自动骨骼检测失败。\n'
                    '需要运行 torchserve 容器才能支持自定义图片。\n'
                    '请先选择内置角色体验动画效果。'
                )
            }), 400
    else:
        # ── preset character path ──────────────────────────────────────────
        char_id = request.form.get('char_id', '1')
        if char_id not in PRESET_CHARS:
            return jsonify({'error': '无效的角色编号'}), 400
        char_cfg_path = str(ROOT / 'examples' / 'characters' / f'char{char_id}' / 'char_cfg.yaml')

    try:
        render_animation(char_cfg_path, motion_key, output_gif)
    except Exception as e:
        logger.error(f'Render failed: {e}', exc_info=True)
        return jsonify({'error': f'渲染失败：{e}'}), 500

    download_url = url_for('download_file', filename=f'{job_id}.gif')
    return jsonify({'gif_url': download_url, 'job_id': job_id})


@app.route('/download/<filename>')
def download_file(filename: str):
    # Prevent path traversal
    safe = Path(filename).name
    return send_from_directory(str(OUTPUT_DIR), safe, as_attachment=True)


@app.route('/preview/<filename>')
def preview_file(filename: str):
    safe = Path(filename).name
    return send_from_directory(str(OUTPUT_DIR), safe)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
