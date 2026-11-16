"""Render an animation that morphs the raw analyzer wakeup graph (wakeup.dot)
into the labelled presentation graph (wakeup_presentation.dot).

Output: wakeup_animation.mp4 and wakeup_animation.gif next to this script.
Requires Pillow and ffmpeg.  Run:  python3 analyzer/make_wakeup_animation.py
"""
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUTPUT_DIRECTORY = Path(__file__).parent
CANVAS_WIDTH, CANVAS_HEIGHT = 1600, 900  # design coordinates
SUPERSAMPLE = 1.6  # design -> pixels (2x of the 1280x720 output)
FRAMES_PER_SECOND = 20
BACKGROUND = (255, 255, 255)

SANS_FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
SANS_BOLD_FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
SERIF_FONT = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"

# Timeline in seconds.
HOLD_PLAIN_END = 1.4
AMBIGUOUS_FADE = (1.4, 2.2)
CAPTION_FADE_IN = (1.4, 1.9)
CAPTION_FADE_OUT = (3.0, 3.5)
MORPH = (3.3, 4.9)
DECORATION_FADE = (4.9, 5.7)
TOTAL_SECONDS = 8.0

# ---- Plain layout: positions (inches) from `dot -Tplain wakeup.dot` ----------
PLAIN_HEIGHT_INCHES = 3.6528
PLAIN_SCALE = 190
PLAIN_OFFSET = (122, 150)
plain_inches = {
    "233740": (4.936, 1.7778), "233853": (0.5355, 2.6389),
    "233854": (2.294, 2.6389), "233855": (2.294, 3.4028),
    "233856": (4.936, 3.1528), "233857": (0.5355, 0.59722),
    "233858": (3.615, 0.25), "233859": (2.294, 0.95833),
    "233860": (6.6008, 0.95833),
}
plain_position = {
    thread_id: (
        PLAIN_OFFSET[0] + x * PLAIN_SCALE,
        PLAIN_OFFSET[1] + (PLAIN_HEIGHT_INCHES - y) * PLAIN_SCALE,
    )
    for thread_id, (x, y) in plain_inches.items()
}

# ---- Clean layout (design pixels); group 1 on top -----------------------------
clean_position = {
    "233853": (330, 330), "233855": (800, 255), "233854": (800, 405), "233856": (1270, 330),
    "233857": (330, 650), "233859": (800, 575), "233858": (800, 725), "233860": (1270, 650),
}
role = {
    "233853": "producer", "233855": "worker 1", "233854": "worker 2", "233856": "sink",
    "233857": "producer", "233859": "worker 1", "233858": "worker 2", "233860": "sink",
}
role_fill = {
    "producer": (219, 234, 254), "worker 1": (254, 243, 199),
    "worker 2": (254, 243, 199), "sink": (220, 252, 231),
}

confident_edges = [  # (source, target, wakeup_count)
    ("233853", "233854", 20), ("233853", "233855", 20),
    ("233854", "233856", 22), ("233855", "233856", 19),
    ("233857", "233858", 20), ("233857", "233859", 20),
    ("233858", "233860", 20), ("233859", "233860", 20),
]
ambiguous_edges = [
    ("233740", "233860", 1), ("233853", "233740", 1), ("233854", "233740", 1),
    ("233859", "233740", 1),
]
cluster_rectangles = {
    "Group 1": (170, 175, 1430, 485),
    "Group 2": (170, 495, 1430, 805),
}

PLAIN_HALF_SIZE = (101.5, 47.5)
CLEAN_HALF_SIZE = (95, 50)
PLAIN_EXPONENT, CLEAN_EXPONENT = 2.0, 4.5  # superellipse: 2 = ellipse, 4.5 = rounded box


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def progress(time_seconds, window):
    start, end = window
    return smoothstep((time_seconds - start) / (end - start))


def lerp(start, end, fraction):
    return start + (end - start) * fraction


def mix_color(color, toward, fraction):
    return tuple(int(round(lerp(c, t, fraction))) for c, t in zip(color, toward))


def fade_color(color, opacity, background=BACKGROUND):
    return mix_color(color, background, 1 - opacity)


fonts = {}


def font(path, size):
    key = (path, size)
    if key not in fonts:
        fonts[key] = ImageFont.truetype(path, int(size * SUPERSAMPLE))
    return fonts[key]


def px(point):
    return (point[0] * SUPERSAMPLE, point[1] * SUPERSAMPLE)


def draw_text(draw, center, text, font_path, size, color, anchor="mm", opacity=1.0):
    if opacity <= 0.01:
        return
    draw.text(px(center), text, font=font(font_path, size), fill=color, anchor=anchor)


def superellipse_points(center, half_size, exponent, count=96):
    points = []
    for index in range(count):
        angle = 2 * math.pi * index / count
        cosine, sine = math.cos(angle), math.sin(angle)
        x = half_size[0] * math.copysign(abs(cosine) ** (2 / exponent), cosine)
        y = half_size[1] * math.copysign(abs(sine) ** (2 / exponent), sine)
        points.append((center[0] + x, center[1] + y))
    return points


def boundary_point(center, half_size, exponent, toward):
    delta_x, delta_y = toward[0] - center[0], toward[1] - center[1]
    scale = (abs(delta_x / half_size[0]) ** exponent
             + abs(delta_y / half_size[1]) ** exponent) ** (-1 / exponent)
    return (center[0] + delta_x * scale, center[1] + delta_y * scale)


def dashed_polyline(draw, points, color, width, dash=12, gap=8):
    draw_on = True
    remaining = dash
    for start, end in zip(points, points[1:]):
        segment_length = math.dist(start, end)
        travelled = 0.0
        while travelled < segment_length:
            step = min(remaining, segment_length - travelled)
            if draw_on:
                a = (lerp(start[0], end[0], travelled / segment_length),
                     lerp(start[1], end[1], travelled / segment_length))
                b = (lerp(start[0], end[0], (travelled + step) / segment_length),
                     lerp(start[1], end[1], (travelled + step) / segment_length))
                draw.line([px(a), px(b)], fill=color, width=max(1, int(width * SUPERSAMPLE)))
            travelled += step
            remaining -= step
            if remaining <= 1e-9:
                draw_on = not draw_on
                remaining = dash if draw_on else gap


def rounded_rectangle_perimeter(left, top, right, bottom, radius, arc_steps=8):
    corners = [
        (right - radius, top + radius, -90), (right - radius, bottom - radius, 0),
        (left + radius, bottom - radius, 90), (left + radius, top + radius, 180),
    ]
    points = []
    for center_x, center_y, start_degrees in corners:
        for step in range(arc_steps + 1):
            angle = math.radians(start_degrees + 90 * step / arc_steps)
            points.append((center_x + radius * math.cos(angle), center_y + radius * math.sin(angle)))
    points.append(points[0])
    return points


def draw_arrow(draw, start, end, color, width, dashed=False, head_length=15, head_width=7):
    length = math.dist(start, end)
    if length < head_length * 1.5:
        return
    unit = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    base = (end[0] - unit[0] * head_length, end[1] - unit[1] * head_length)
    if dashed:
        dashed_polyline(draw, [start, base], color, width)
    else:
        draw.line([px(start), px(base)], fill=color, width=max(1, int(width * SUPERSAMPLE)))
    normal = (-unit[1], unit[0])
    draw.polygon(
        [px(end), px((base[0] + normal[0] * head_width, base[1] + normal[1] * head_width)),
         px((base[0] - normal[0] * head_width, base[1] - normal[1] * head_width))],
        fill=color,
    )


def render_frame(time_seconds):
    image = Image.new("RGB", (int(CANVAS_WIDTH * SUPERSAMPLE), int(CANVAS_HEIGHT * SUPERSAMPLE)), BACKGROUND)
    draw = ImageDraw.Draw(image)

    morph = progress(time_seconds, MORPH)
    ambiguous_opacity = 1 - progress(time_seconds, AMBIGUOUS_FADE)
    decoration_opacity = progress(time_seconds, DECORATION_FADE)
    caption_opacity = (progress(time_seconds, CAPTION_FADE_IN)
                       * (1 - progress(time_seconds, CAPTION_FADE_OUT)))
    old_text_opacity = 1 - smoothstep(morph / 0.4)
    new_text_opacity = smoothstep((morph - 0.6) / 0.4)

    half_size = (lerp(PLAIN_HALF_SIZE[0], CLEAN_HALF_SIZE[0], morph),
                 lerp(PLAIN_HALF_SIZE[1], CLEAN_HALF_SIZE[1], morph))
    exponent = lerp(PLAIN_EXPONENT, CLEAN_EXPONENT, morph)

    current_position = {}
    for thread_id, plain in plain_position.items():
        clean = clean_position.get(thread_id, plain)
        current_position[thread_id] = (lerp(plain[0], clean[0], morph), lerp(plain[1], clean[1], morph))

    # Header text: raw output -> presentation title.
    raw_header = "Raw analyzer output (wakeup method)"
    draw_text(draw, (800, 70), raw_header, SANS_BOLD_FONT, 34, fade_color((30, 30, 30), old_text_opacity), opacity=old_text_opacity)
    draw_text(draw, (800, 62), "Thread wakeup graph inferred from kernel sched_waking events",
              SANS_BOLD_FONT, 34, fade_color((30, 30, 30), decoration_opacity), opacity=decoration_opacity)
    draw_text(draw, (800, 112), "edge A → B = A woke B  ·  label = wakeup count",
              SANS_FONT, 24, fade_color((90, 90, 90), decoration_opacity), opacity=decoration_opacity)

    # Group boxes.
    if decoration_opacity > 0:
        for name, (left, top, right, bottom) in cluster_rectangles.items():
            dashed_polyline(draw, rounded_rectangle_perimeter(left, top, right, bottom, 26),
                            fade_color((150, 150, 150), decoration_opacity), 2.5)
            draw_text(draw, (left + 28, top + 26), name, SANS_FONT, 24,
                      fade_color((90, 90, 90), decoration_opacity), anchor="lm", opacity=decoration_opacity)

    edge_color = mix_color((0, 0, 0), (68, 68, 68), morph)
    edge_width = lerp(2.0, 3.4, morph)

    def edge_endpoints(source, target):
        source_center, target_center = current_position[source], current_position[target]
        start = boundary_point(source_center, half_size, exponent, target_center)
        end = boundary_point(target_center, half_size, exponent, source_center)
        return start, end

    def edge_label(source, target, count, color, opacity, dashed_edge):
        start, end = edge_endpoints(source, target)
        length = math.dist(start, end) or 1
        normal = (-(end[1] - start[1]) / length, (end[0] - start[0]) / length)
        if normal[1] > 0:
            normal = (-normal[0], -normal[1])
        middle = ((start[0] + end[0]) / 2 + normal[0] * 18, (start[1] + end[1]) / 2 + normal[1] * 18)
        draw_text(draw, middle, str(count), SERIF_FONT, 24, fade_color(color, old_text_opacity * opacity), opacity=old_text_opacity * opacity)
        draw_text(draw, middle, str(count), SANS_FONT, 24, fade_color(color, new_text_opacity * opacity), opacity=new_text_opacity * opacity)

    # Ambiguous edges (fade out first).
    if ambiguous_opacity > 0:
        for source, target, count in ambiguous_edges:
            start, end = edge_endpoints(source, target)
            color = fade_color((0, 0, 0), ambiguous_opacity)
            draw_arrow(draw, start, end, color, 2.0, dashed=True)
            edge_label(source, target, count, (0, 0, 0), ambiguous_opacity, True)

    for source, target, count in confident_edges:
        start, end = edge_endpoints(source, target)
        draw_arrow(draw, start, end, edge_color, edge_width)
        edge_label(source, target, count, edge_color, 1.0, False)

    # Nodes.
    for thread_id, center in current_position.items():
        opacity = 1.0 if thread_id in clean_position else ambiguous_opacity
        if opacity <= 0:
            continue
        fill = (255, 255, 255)
        if thread_id in clean_position:
            fill = mix_color((255, 255, 255), role_fill[role[thread_id]], morph)
        outline = fade_color((0, 0, 0), opacity)
        points = [px(point) for point in superellipse_points(center, half_size, exponent)]
        draw.polygon(points, fill=fade_color(fill, opacity))
        draw.line(points + [points[0]], fill=outline, width=max(1, int(2.4 * SUPERSAMPLE)), joint="curve")
        draw_text(draw, center, thread_id, SERIF_FONT, 28,
                  fade_color((0, 0, 0), old_text_opacity * opacity, fill), opacity=old_text_opacity * opacity)
        if thread_id in clean_position:
            draw_text(draw, (center[0], center[1] - 14), role[thread_id], SANS_BOLD_FONT, 26,
                      fade_color((20, 20, 20), new_text_opacity, fill), opacity=new_text_opacity)
            draw_text(draw, (center[0], center[1] + 17), f"({thread_id})", SANS_FONT, 22,
                      fade_color((70, 70, 70), new_text_opacity, fill), opacity=new_text_opacity)

    # Caption explaining the dropped edges.
    draw_text(draw, (800, 868),
              "4 ambiguous edges (each seen once, below the 5-wakeup threshold) are dropped",
              SANS_FONT, 26, fade_color((120, 40, 40), caption_opacity), opacity=caption_opacity)

    return image.resize((CANVAS_WIDTH * 4 // 5, CANVAS_HEIGHT * 4 // 5), Image.LANCZOS)


def main():
    frame_count = int(TOTAL_SECONDS * FRAMES_PER_SECOND)
    with tempfile.TemporaryDirectory() as frame_directory:
        for index in range(frame_count):
            render_frame(index / FRAMES_PER_SECOND).save(Path(frame_directory) / f"frame_{index:04d}.png")
        pattern = str(Path(frame_directory) / "frame_%04d.png")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FRAMES_PER_SECOND), "-i", pattern,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16",
             str(OUTPUT_DIRECTORY / "wakeup_animation.mp4")], check=True)
        palette = Path(frame_directory) / "palette.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FRAMES_PER_SECOND),
                        "-i", pattern, "-vf", "palettegen=max_colors=96", str(palette)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FRAMES_PER_SECOND),
                        "-i", pattern, "-i", str(palette), "-lavfi", "paletteuse=dither=none",
                        "-loop", "0", str(OUTPUT_DIRECTORY / "wakeup_animation.gif")], check=True)
        # Keep a few still frames for checking the result.
        for label, time_seconds in (("start", 0.0), ("middle", 4.1), ("end", TOTAL_SECONDS - 0.05)):
            shutil.copy(Path(frame_directory) / f"frame_{int(time_seconds * FRAMES_PER_SECOND):04d}.png",
                        OUTPUT_DIRECTORY / f"wakeup_animation_{label}.png")


if __name__ == "__main__":
    main()
