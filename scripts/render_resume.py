#!/usr/bin/env python3
"""Render verified resume data in the fixed three-paragraph, single-column style."""
import argparse
import json
import re
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ORDER = ('科研经历', '项目经历', '实习经历')
LABELS = (('background', '项目背景：'), ('work', '我的工作：'), ('results', '项目成果：'))
BLUE = colors.HexColor('#2F5496')
W, H = A4
MARGIN = 36.0


def text(value, label, required=False):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string')
    if required and not value.strip():
        raise ValueError(f'{label} is required; supply verified facts before rendering')
    return value.strip()


def validate(data):
    profile = data.get('profile', {})
    text(profile.get('name', ''), 'profile.name', True)
    lines = profile.get('contact_lines', [])
    if not isinstance(lines, list) or len(lines) > 2:
        raise ValueError('profile.contact_lines must contain at most two strings')
    for line in lines:
        text(line, 'contact line', True)
    for school in data.get('education', []):
        for key in ('institution', 'degree'):
            text(school.get(key, ''), f'education.{key}', True)
        text(school.get('dates', ''), 'education.dates')
        if not isinstance(school.get('details', []), list):
            raise ValueError('education.details must be a list of strings')
        for detail in school.get('details', []):
            text(detail, 'education detail', True)
    seen = set()
    for section in data.get('sections', []):
        title = section.get('title')
        if title not in ORDER or title in seen:
            raise ValueError(f'Unsupported or duplicate section: {title}')
        seen.add(title)
        entries = section.get('entries', [])
        if not isinstance(entries, list) or not entries:
            raise ValueError(f'{title} must have entries; omit empty sections')
        for entry in entries:
            text(entry.get('name', ''), 'entry.name', True)
            for optional in ('dates', 'role'):
                text(entry.get(optional, ''), f'entry.{optional}')
            for key, label in LABELS:
                value = text(entry.get(key, ''), f'{title}.{key}', True)
                if re.search(r'(?m)^\s*(?:[-*]\s|[•●▪])', value):
                    raise ValueError(f'{label} must be continuous prose, not bullet points')
                if any(tag in value for _, tag in LABELS):
                    raise ValueError(f'{key} must contain prose only; labels are automatic')
    for key in ('skills', 'achievements', 'evaluation'):
        text(data.get('summary', {}).get(key, ''), f'summary.{key}')
    text(data.get('honors', ''), 'honors')


def rich(value):
    """Accept only **bold**; escape any HTML-like source text."""
    chunks = re.split(r'(\*\*.*?\*\*)', re.sub(r'\s*\n\s*', ' ', value))
    return ''.join('<b>' + escape(s[2:-2]) + '</b>'
                   if s.startswith('**') and s.endswith('**') else escape(s)
                   for s in chunks)


def prepare_logos(data, base):
    """Resolve caller-verified company assets; never fetch or invent a logo."""
    for section in data.get('sections', []):
        for entry in section['entries']:
            path = entry.get('company_logo')
            if not path:
                continue
            source = text(entry.get('company_logo_source', ''), 'company_logo_source', True)
            if source != 'user-provided' and not source.startswith(('https://', 'http://')):
                raise ValueError('Record the verified official logo source URL or user-provided')
            path = Path(text(path, 'company_logo', True))
            if not path.is_absolute():
                path = base / path
            if not path.is_file() or path.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
                raise ValueError('company_logo must be an existing PNG/JPEG; convert official SVG first')
            logo_size(path)
            entry['company_logo'] = str(path.resolve())


def logo_size(path, max_width=24.0, max_height=10.5):
    width, height = ImageReader(str(path)).getSize()
    if width <= 0 or height <= 0:
        raise ValueError('The company logo has invalid dimensions')
    scale = min(max_width / width, max_height / height)
    return width * scale, height * scale


class Layout:
    def __init__(self, data, font_size, photo):
        self.data, self.size, self.photo = data, font_size, photo
        self.leading = font_size + 3.7
        self.y = 101.0
        self.ops = []
        self.entries = 0

    def right(self):
        # A portrait extends into the education area in the reference design.
        return W - MARGIN - 92.0 if self.photo and self.y < 162.0 else W - MARGIN

    def paragraph(self, value, label=''):
        available = self.right() - MARGIN
        style = ParagraphStyle('body', fontName='ResumeRegular', fontSize=self.size,
                               leading=self.leading, wordWrap='CJK', textColor=colors.black)
        markup = ('<b>' + escape(label) + '</b>' if label else '') + rich(value)
        para = Paragraph(markup, style)
        _, height = para.wrap(available, H)
        self.ops.append(('paragraph', MARGIN, self.y, height, para))
        self.y += height + 0.7

    def heading(self, title):
        right = self.right()
        self.ops.append(('heading', title, self.y, right))
        self.y += 18.5

    def row(self, values, education=False, logo=None):
        available = self.right() - MARGIN
        ratios = (0.43, 0.32, 0.25) if education else (0.22, 0.53, 0.25)
        alignments = (0, 1, 2)
        columns, x, heights = [], MARGIN, []
        for index, (value, ratio, alignment) in enumerate(zip(values, ratios, alignments)):
            width = available * ratio
            style = ParagraphStyle('row', fontName='ResumeBold', fontSize=self.size,
                                   leading=self.leading, wordWrap='CJK', alignment=alignment)
            markup = rich(value)
            if index == 1 and logo:
                image_width, image_height = logo_size(logo)
                markup = (f'<img src="{escape(str(logo), quote=True)}" width="{image_width}" '
                          f'height="{image_height}" valign="middle"/>&nbsp;' + markup)
            para = Paragraph(markup, style)
            _, height = para.wrap(width - 5, H)
            columns.append((x, width - 5, para, height))
            heights.append(height)
            x += width
        self.ops.append(('row', self.y, columns))
        self.y += max(heights or [0]) + 1.8

    def build(self):
        if self.data.get('education'):
            self.heading('教育背景')
            for school in self.data['education']:
                self.row((school['institution'], school['degree'], school.get('dates', '')), True)
                for detail in school.get('details', []):
                    self.paragraph(detail)
            if self.data.get('honors'):
                self.paragraph(self.data['honors'], '荣誉奖项：')
        sections = {s['title']: s for s in self.data.get('sections', [])}
        for title in ORDER:
            if title not in sections:
                continue
            self.heading(title)
            for entry in sections[title]['entries']:
                self.row((entry.get('dates', ''), entry['name'], entry.get('role', '')),
                         logo=entry.get('company_logo'))
                for key, label in LABELS:
                    self.paragraph(entry[key], label)
                self.entries += 1
                self.y += 1.5
        summary = self.data.get('summary', {})
        if any(summary.get(k) for k in ('skills', 'achievements', 'evaluation')):
            self.heading('个人技能与自我评价')
            for key, label in (('skills', '个人技能：'), ('achievements', '成果总结：'),
                               ('evaluation', '自我评价：')):
                if summary.get(key):
                    self.paragraph(summary[key], label)
        return self

    def draw(self, output):
        c = canvas.Canvas(str(output), pagesize=A4)
        c.setTitle(self.data['profile']['name'] + ' 个人简历')
        c.setAuthor('')
        center = (MARGIN + self.right()) / 2
        if self.photo:
            c.drawImage(ImageReader(str(self.photo)), W - MARGIN - 86, H - 158,
                        width=86, height=120, preserveAspectRatio=True, anchor='c', mask='auto')
            center = (MARGIN + W - MARGIN - 92) / 2
        profile = self.data['profile']
        c.setFont('ResumeBold', 20)
        c.setFillColor(BLUE)
        c.drawCentredString(center, H - 58, profile['name'])
        for n, line in enumerate(profile.get('contact_lines', [])):
            # Contacts may contain supplied **labels**; fit within the name region.
            style = ParagraphStyle('contact', fontName='ResumeRegular', fontSize=10.8,
                                   leading=13.8, alignment=1, textColor=colors.black)
            contact = rich(line).replace('<b>', '<font color="#2F5496"><b>').replace('</b>', '</b></font>')
            para = Paragraph(contact, style)
            contact_width = (W - 2 * MARGIN - (92 if self.photo else 0))
            _, height = para.wrap(contact_width, 20)
            if height > 15:
                raise ValueError('A contact line wraps; shorten the two header lines')
            para.drawOn(c, MARGIN, H - 68 - n * 14 - height)
        for op in self.ops:
            if op[0] == 'heading':
                _, title, y, right = op
                c.setFont('ResumeBold', 12)
                c.setFillColor(BLUE)
                c.drawString(MARGIN, H - y - 10.8, title)
                c.setLineWidth(1.8)
                c.setStrokeColor(colors.black)
                c.line(MARGIN, H - y - 13.1, right, H - y - 13.1)
            elif op[0] == 'paragraph':
                _, x, y, height, para = op
                para.drawOn(c, x, H - y - height)
            elif op[0] == 'row':
                _, y, columns = op
                for x, width, para, height in columns:
                    para.drawOn(c, x, H - y - height)
        c.showPage()
        c.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--font', type=Path, required=True, help='Embeddable Chinese regular TTF')
    parser.add_argument('--bold-font', type=Path, required=True, help='Embeddable Chinese bold TTF')
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding='utf-8'))
    validate(data)
    prepare_logos(data, args.input.parent)
    regular = TTFont('ResumeRegular', str(args.font))
    bold = TTFont('ResumeBold', str(args.bold_font))
    # Variable-font instances can retain the same PostScript name. Keep their
    # embedded faces distinct so bold labels do not silently become regular.
    regular.face.name = b'ResumeRegularEmbedded'
    bold.face.name = b'ResumeBoldEmbedded'
    pdfmetrics.registerFont(regular)
    pdfmetrics.registerFont(bold)
    pdfmetrics.registerFontFamily('ResumeRegular', normal='ResumeRegular', bold='ResumeBold',
                                 italic='ResumeRegular', boldItalic='ResumeBold')
    photo = data['profile'].get('photo')
    if photo:
        photo = Path(photo)
        if not photo.is_absolute():
            photo = args.input.parent / photo
        if not photo.is_file():
            raise ValueError('The supplied portrait file does not exist')
    selected = None
    for size in (10.2, 10.0, 9.8):
        candidate = Layout(data, size, photo).build()
        if candidate.y <= H - MARGIN:
            selected = candidate
            break
    if selected is None:
        raise ValueError('Content exceeds one readable A4 page. Shorten repetition while keeping '
                         'all three paragraphs, or use a matching multi-page document workflow.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    selected.draw(args.output)
    print(json.dumps({'pages': 1, 'font_size': selected.size, 'leading': selected.leading,
                      'entries': selected.entries, 'content_bottom_pt': round(selected.y, 2)},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
