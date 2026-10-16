#!/usr/bin/env python3
"""Plot collected comparison XMLs and audit their summaries (requires matplotlib)."""

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
import html
import hashlib
import json
import math
import os
import re
import random
from pathlib import Path
import statistics
import shlex
import sys
import tempfile
import xml.etree.ElementTree as ET


PORTFOLIO = 'theoretical-best'
# LLNCS uses 12.2 cm text width and 9 TeX pt captions. Matplotlib uses
# PostScript points (72/in), whereas TeX points are 72.27/in.
LNCS_WIDTH_IN = 122 / 25.4
LNCS_CAPTION_SIZE = 9 * 72 / 72.27
LNCS_SCRIPT_SIZE = 7 * 72 / 72.27


@dataclass(slots=True)
class Result:
    status: str
    outcome: str
    seconds: float | None

    @property
    def eligible(self):
        return self.outcome != 'excluded'

    @property
    def solved(self):
        return self.status == 'true'


def read_results(path, budget):
    """Stream XML, retaining small per-theorem records, not the XML tree."""
    rows = {}
    counts = defaultdict(Counter)
    examples = defaultdict(list)
    issues = Counter()
    prover = None
    root = None
    for event, elem in ET.iterparse(path, events=('start', 'end')):
        if root is None:
            root = elem
        if event != 'end' or elem.tag != 'run':
            continue
        cols = {c.get('title'): c.get('value', '') for c in elem.findall('column')}
        reported = cols.get('theorem')
        match = re.search(r'(?:^| )--theorem (.+)$', elem.get('options', ''))
        task_match = re.search(r'#\d+-(.+)$', elem.get('name', ''))
        name = shlex.split(match[1])[0] if match else task_match[1] if task_match else reported
        if not name:
            raise ValueError(f'{path}: cannot identify run {elem.get("name")}')
        if not reported:
            counts['metadata']['theorem_recovered_from_task_id'] += 1
        elif reported != name:
            counts['metadata']['theorem_mismatch'] += 1
            examples['theorem_mismatch'].append(dict(task_theorem=name, reported_theorem=reported,
                                                    status=cols.get('status')))
        current = cols.get('prover')
        if not current or (prover is not None and current != prover):
            raise ValueError(f'{path}: missing or inconsistent prover')
        prover = current
        if name in rows:
            raise ValueError(f'{path}: duplicate theorem {name}; do not pool repetitions')
        raw = cols.get('hammer-seconds')
        try:
            seconds = float(raw) if raw else None
        except ValueError:
            seconds = None
            issues['invalid_time'] += 1
        if seconds is not None and (not math.isfinite(seconds) or seconds < 0):
            issues['invalid_time'] += 1
            seconds = None
        row = Result(cols.get('status', ''), cols.get('outcome', ''), seconds)
        if not row.status or not row.outcome:
            issues['missing_status_or_outcome'] += 1
        if row.solved != (row.outcome == 'proved'):
            issues['status_outcome_mismatch'] += 1
        if row.solved:
            if cols.get('kernel-checked', '').lower() != 'true':
                issues['success_without_kernel_check'] += 1
            if seconds is None:
                issues['success_without_time'] += 1
            elif seconds > budget:
                issues['success_over_budget'] += 1
        rows[name] = row
        counts['status'][row.status] += 1
        counts['outcome'][row.outcome] += 1
        counts['category'][cols.get('category') or row.outcome] += 1
        if row.eligible:
            counts['eligible_status'][row.status] += 1
        if row.solved:
            counts['successful_route'][cols.get('route', '')] += 1
        if row.status.startswith(('ERROR', 'TIMEOUT')):
            category = cols.get('category') or row.status
            message = cols.get('error') or cols.get('lean-error') or '(no message recorded)'
            counts['error_messages'][message] += 1
            if len(examples[category]) < 5:
                examples[category].append({'theorem': name, 'status': row.status,
                                          'error': message})
        elem.clear()
        root.clear()
    if not rows:
        raise ValueError(f'{path}: no runs')
    diagnostics = {k: dict(v.most_common()) for k, v in counts.items()}
    diagnostics.update(issues=dict(issues), error_examples=dict(examples), rows=len(rows))
    return prover, rows, diagnostics


def summarize(data, provers):
    common = set.intersection(*(set(data[p]) for p in provers))
    eligible = {t for t in common if all(data[p][t].eligible for p in provers)}
    solved = {p: {t for t in eligible if data[p][t].solved} for p in provers}
    report = dict(provers=provers, common_completed=len(common), common_eligible=len(eligible),
                  incomplete=len(set.union(*(set(data[p]) for p in provers))) - len(common),
                  excluded=len(common) - len(eligible),
                  proved={p: len(solved[p]) for p in provers},
                  proved_by_any=len(set.union(*solved.values())),
                  proved_by_all=len(set.intersection(*solved.values())),
                  only_this_prover={p: len(solved[p] - set().union(
                      *(solved[q] for q in provers if q != p))) for p in provers},
                  pairwise={p: {q: dict(both=len(solved[p] & solved[q]),
                                       only_first=len(solved[p] - solved[q]),
                                       only_second=len(solved[q] - solved[p]))
                                for q in provers if q != p} for p in provers})
    return report, eligible, solved


def write_csv(path, fields, rows):
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)


def markdown_table(fields, rows):
    def escape(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    return '\n'.join(['| ' + ' | '.join(map(escape, fields)) + ' |',
                      '| ' + ' | '.join(['---'] * len(fields)) + ' |',
                      *('| ' + ' | '.join(map(escape, row)) + ' |' for row in rows)])


def portfolio_results(data, provers, tasks):
    """Oracle portfolio: fastest accepted proof per theorem in this run."""
    rows = {}
    for t in tasks:
        times = [data[p][t].seconds for p in provers if data[p][t].solved]
        rows[t] = Result('true', 'proved', min(times)) if times else Result(
            'unknown', 'portfolio_unsolved', None)
    return rows


TOOL_LABELS = {
    'vampire-hammer': 'VH', 'lean-auto-duper': 'LD', 'duper': 'DU',
    'aesop': 'AE', 'simp_all': 'SA', 'grind': 'GR', 'lean-smt': 'LS', PORTFOLIO: 'TB',
}

ABBREVIATION_CAPTION = (
    'VH = Vampire-Hammer; LD = Lean-auto duper; DU = Duper; AE = Aesop; '
    'SA = SimpAll; GR = Grind; LS = Lean-SMT; TB = Theoretical best.')


# Paul Tol bright colors, with dark gray for the seventh tool. Distinct dash
# patterns supplement color on cactus plots; the virtual portfolio stays black.
TOOL_COLORS = {
    'vampire-hammer': '#4477AA', 'lean-auto-duper': '#EE6677',
    'duper': '#228833', 'aesop': '#AA3377', 'simp_all': '#CCBB44',
    'grind': '#66CCEE', 'lean-smt': '#555555', PORTFOLIO: '#000000',
}


CACTUS_CAPTION = (
    'Compact cactus figure: plot on the left and a scriptsize table on the right. '
    'The number above the table is the total matched eligible instances in this comparison. '
    'The logarithmic time axis starts at one millisecond; the horizontal dashed line marks the timeout. '
    'Solved is the mean number solved per run. Unique counts theorems solved by that tool '
    'and none of the other real solvers in this comparison. Counts are computed '
    'within each run before averaging; the virtual theoretical portfolio is excluded '
    'from uniqueness, and its own Unique entry is not applicable. '
    'Counts in the figure are rounded to integers; exact averages remain in '
    'the report tables and CSVs. Line samples in the table identify the curves. '
    + ABBREVIATION_CAPTION)


def small_caps_font(size):
    from matplotlib.font_manager import FontProperties
    return FontProperties(family='LMRomanCaps10', size=size)


def cactus_table_rows(output):
    """Solved and unique counts against every other real solver."""
    with (output / 'comparison.csv').open(newline='') as stream:
        records = list(csv.DictReader(stream))
    return [(r.get('prover', r.get('Prover')), float(r.get('mean_solved', r.get('Solved'))),
             float(value) if (value := r.get('mean_unique', r.get('Unique among selected'))) else None)
            for r in records]


def render_cactus(curves, output, label, eligible, budget, colors):
    """Publication exports with small-caps tool labels and exact curve values."""
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'hammer-matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import LogFormatterMathtext, MaxNLocator, NullFormatter, StrMethodFormatter

    style = {
        'font.family': 'serif', 'font.serif': ['LMRoman9'],
        'mathtext.fontset': 'cm', 'font.size': LNCS_CAPTION_SIZE, 'axes.labelsize': LNCS_CAPTION_SIZE,
        'xtick.labelsize': LNCS_CAPTION_SIZE, 'ytick.labelsize': LNCS_CAPTION_SIZE, 'axes.linewidth': .65,
        'xtick.major.width': .65, 'ytick.major.width': .65,
        'xtick.minor.width': .45, 'ytick.minor.width': .45,
        'pdf.fonttype': 42, 'ps.fonttype': 42, 'svg.fonttype': 'none',
        'savefig.facecolor': 'white', 'figure.facecolor': 'white',
    }
    repetitions = len(next(iter(curves.values())))
    table_rows = cactus_table_rows(output)
    patterns = ['-', (0, (5, 2)), (0, (3, 1.5, 1, 1.5)), (0, (1, 1.5)),
                (0, (7, 2, 1, 2)), (0, (3, 2)), (0, (6, 1.5, 1, 1.5, 1, 1.5))]
    with plt.rc_context(style):
        for scale in ('linlog', 'linear'):
            fig = plt.figure(figsize=(LNCS_WIDTH_IN, 60 / 25.4))
            ax = fig.add_axes([.125, .21, .55, .76])
            table_ax = fig.add_axes([.705, .04, .285, .85])
            table_ax.set_axis_off()
            for index, (p, run_times) in enumerate(curves.items()):
                def displayed_times(values):
                    return [max(t, 1e-6) if scale == 'linlog' else t for t in values]
                if repetitions > 1:
                    for times in run_times:
                        ax.step(range(1, len(times) + 1), displayed_times(times), where='post',
                                color=colors[p], linewidth=.5, alpha=.16, zorder=1)
                points = average_curve(run_times)
                if not points:
                    continue
                x, y = zip(*points)
                ax.step(x, displayed_times(y), where='post', color=colors[p],
                        linewidth=1.2,
                        linestyle=(0, (1.5, 1.5)) if p == PORTFOLIO else patterns[index % len(patterns)],
                        label=TOOL_LABELS.get(p, p), zorder=3 if p == PORTFOLIO else 2)
            ax.set(xlabel='Solved theorems',
                   ylabel='Proof time (s)', xscale='linear',
                   yscale='log' if scale == 'linlog' else 'linear')
            ax.set_xlim(left=0)
            if scale == 'linear':
                ax.set_ylim(0, budget * 1.02)
            else:
                ax.set_ylim(bottom=1e-3, top=budget * 1.12)
                ax.yaxis.set_minor_formatter(NullFormatter())
            ax.xaxis.set_major_locator(MaxNLocator(nbins=3, integer=True, steps=[1, 2, 2.5, 5, 10]))
            if scale == 'linear':
                ax.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
            ax.xaxis.set_major_formatter(StrMethodFormatter('{x:,.0f}'))
            ax.yaxis.set_major_formatter(LogFormatterMathtext() if scale == 'linlog'
                                        else StrMethodFormatter('{x:g}'))
            ax.spines[['top', 'right']].set_visible(False)
            ax.tick_params(which='both', direction='out', top=False, right=False)
            ax.set_axisbelow(True)
            ax.grid(which='major', color='#b0b0b0', linewidth=.5, alpha=.35)
            ax.axhline(budget, color='#777777', linewidth=.65, linestyle=(0, (4, 3)), zorder=1)
            ax.annotate('timeout', xy=(1, budget), xycoords=('axes fraction', 'data'),
                        xytext=(5, 0), textcoords='offset points', ha='left', va='center',
                        fontsize=LNCS_SCRIPT_SIZE, color='#555555', annotation_clip=False)
            # The compact scriptsize table also identifies the curve styles.
            table_ax.set(xlim=(0, 1), ylim=(0, 1))
            text_style = dict(fontsize=LNCS_SCRIPT_SIZE, fontfamily='LMRoman7', va='center')
            table_ax.text(.5, 1.01, 'Total instances:', ha='center', **text_style)
            table_ax.text(.5, .95, f'{eligible:,}', ha='center', **text_style)
            table_ax.plot([0, 1], [.88, .88], color='black', linewidth=.6)
            for x, header, align in ((.11, 'Solver', 'left'), (.67, 'Solved', 'right'),
                                     (.99, 'Unique', 'right')):
                table_ax.text(x, .83, header, ha=align, **text_style)
            table_ax.plot([0, 1], [.785, .785], color='black', linewidth=.4)
            for index, (p, count, unique) in enumerate(table_rows):
                y = .73 - index * .075
                line_style = (0, (1.5, 1.5)) if p == PORTFOLIO else patterns[index % len(patterns)]
                table_ax.plot([0, .075], [y, y], color=colors[p], linewidth=1.2, linestyle=line_style)
                name_label = TOOL_LABELS.get(p, p)
                name = table_ax.text(.11, y, name_label, ha='left', **text_style)
                if p != PORTFOLIO:
                    name.set_fontproperties(small_caps_font(LNCS_SCRIPT_SIZE))
                table_ax.text(.67, y, f'{math.floor(count + .5):,}', ha='right', **text_style)
                table_ax.text(.99, y, f'{math.floor(unique + .5):,}' if unique is not None else '—',
                              ha='right', **text_style)
                if p == PORTFOLIO:
                    table_ax.plot([0, 1], [y + .041, y + .041], color='#777777', linewidth=.35)
            bottom = .73 - (len(table_rows) - 1) * .075 - .05
            table_ax.plot([0, 1], [bottom, bottom], color='black', linewidth=.6)
            for extension in ('pdf', 'svg', 'png'):
                fig.savefig(output / f'cactus-{scale}.{extension}', dpi=300)
            plt.close(fig)


def scatter_points(runs, provers, baseline, tasks):
    """Success-conditioned means; omit only cases neither tool ever solves."""
    points = {}
    for p in provers:
        if p in (baseline, PORTFOLIO):
            continue
        paired = []
        for t in sorted(tasks):
            x = [run[baseline][t].seconds for run in runs if run[baseline][t].solved]
            y = [run[p][t].seconds for run in runs if run[p][t].solved]
            if x or y:
                paired.append((t, statistics.mean(x) if x else None,
                               statistics.mean(y) if y else None, len(x), len(y)))
        points[p] = paired
    return points


def unsolved_coordinate(theorem, prover, axis, center):
    """Stable jitter within the categorical band; never applied to real times."""
    key = '\0'.join((theorem, prover, axis)).encode()
    fraction = int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), 'big') / (2**64 - 1)
    return center * 10 ** ((fraction - .5) * .8)


def plot_scatter(runs, provers, baseline, tasks, output, budget, colors):
    """Combined opponents on equal log scales with the y=x reference."""
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'hammer-matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import LogFormatterMathtext, LogLocator, NullFormatter

    points = scatter_points(runs, provers, baseline, tasks)
    if not points:
        return
    write_csv(output / 'scatter-data.csv',
              ['prover', 'theorem', 'baseline_mean_seconds', 'prover_mean_seconds',
               'baseline_successful_runs', 'prover_successful_runs', 'repetitions'],
              ((p, t, x, y, nx, ny, len(runs)) for p, records in points.items() for t, x, y, nx, ny in records))
    positive = [max(v, 1e-6) for records in points.values() for _, x, y, _, _ in records
                for v in (x, y) if v is not None]
    lower = 10 ** math.floor(math.log10(min(positive))) if positive else 1e-4
    # This coordinate is a category, never a measured or averaged failure time.
    unsolved = 10 ** (math.ceil(math.log10(budget)) + 1)
    upper = unsolved * 3
    class CensoredLogFormatter(LogFormatterMathtext):
        def __call__(self, value, pos=None):
            return 'unsolved' if math.isclose(value, unsolved) else super().__call__(value, pos)
    style = {'font.family': 'serif', 'font.serif': ['LMRoman9'],
             'mathtext.fontset': 'cm', 'font.size': LNCS_CAPTION_SIZE, 'axes.labelsize': LNCS_CAPTION_SIZE,
             'xtick.labelsize': LNCS_CAPTION_SIZE, 'ytick.labelsize': LNCS_CAPTION_SIZE, 'axes.linewidth': .65,
             'pdf.fonttype': 42, 'ps.fonttype': 42, 'svg.fonttype': 'none'}
    with plt.rc_context(style):
        from matplotlib.lines import Line2D
        fig, ax = plt.subplots(figsize=(LNCS_WIDTH_IN, 110 / 25.4), layout='constrained')
        # Interleave tools deterministically so a tool drawn last cannot mask
        # all other colors in the densest regions of the combined plot.
        clouds = {False: [], True: []}
        for p, records in points.items():
            for t, x, y, nx, ny in records:
                mixed = any(0 < n < len(runs) for n in (nx, ny))
                clouds[mixed].append((max(x, 1e-6) if x is not None else unsolved_coordinate(t, p, 'x', unsolved),
                                      max(y, 1e-6) if y is not None else unsolved_coordinate(t, p, 'y', unsolved),
                                      colors[p]))
        for mixed, marker in ((False, 'o'), (True, '^')):
            cloud = clouds[mixed]
            random.Random(0).shuffle(cloud)
            if cloud:
                x, y, point_colors = zip(*cloud)
                ax.scatter(x, y, s=8 if mixed else 2, alpha=.25 if mixed else .08,
                           marker=marker, c=point_colors, edgecolors='none', rasterized=True)
        ax.plot([lower, budget], [lower, budget], color='#555555', linewidth=.8,
                linestyle='--', zorder=0)
        boundary = math.sqrt(budget * unsolved)
        ax.axvspan(boundary, upper, color='#777777', alpha=.04)
        ax.axhspan(boundary, upper, color='#777777', alpha=.04)
        ax.set(xscale='log', yscale='log', xlim=(lower, upper), ylim=(lower, upper),
               xlabel=TOOL_LABELS.get(baseline, baseline) + r' ($\mathrm{s}$)',
               ylabel='Other tool (s)')
        ax.xaxis.label.set_fontproperties(small_caps_font(LNCS_CAPTION_SIZE))
        ax.set_aspect('equal', adjustable='box')
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_locator(LogLocator(base=10, numticks=7))
            axis.set_major_formatter(CensoredLogFormatter())
            axis.set_minor_formatter(NullFormatter())
        ax.spines[['top', 'right']].set_visible(False)
        ax.set_axisbelow(True)
        ax.grid(which='major', linewidth=.45, color='#b0b0b0', alpha=.3)
        handles = [Line2D([], [], linestyle='none', marker='o', markersize=4,
                          color=colors[p], label=TOOL_LABELS.get(p, p)) for p in points]
        legend = ax.legend(handles=handles, loc='lower right', framealpha=.88,
                           edgecolor='#dddddd', facecolor='white', fontsize=LNCS_CAPTION_SIZE,
                           handlelength=1.3, handletextpad=.5, labelspacing=.4)
        for text in legend.get_texts():
            text.set_fontproperties(small_caps_font(LNCS_CAPTION_SIZE))
        for ext in ('pdf', 'svg', 'png'):
            fig.savefig(output / f'scatter-vs-baseline.{ext}', dpi=300, facecolor='white')
        plt.close(fig)


SCATTER_CAPTION = (
    'Scatter plots combine all real opponents in one color-coded plot. Each point is a theorem/tool pair '
    'where at least one tool solved the theorem in at least one run, using this scope’s common eligible set; '
    'pairs neither tool ever solved are excluded. Times are arithmetic means over successful runs only. A tool with no successful run is '
    'placed in the shaded “unsolved” band, a categorical position beyond the budget rather '
    'than a measured time. Reproducible jitter along the unsolved axis reveals overlapping '
    'points; measured time coordinates are unchanged. Triangles mark mixed success across runs. vampire-hammer is on '
    'the x-axis with the default baseline. The dashed line is equal time: points above '
    'it favor the baseline, and points below favor the other tool. Zero times are displayed '
    'at one microsecond on logarithmic axes. ' + ABBREVIATION_CAPTION)

def plot_cactus(data, report, solved, output, label, budget, colors):
    curves = {p: [sorted(data[p][t].seconds for t in solved[p])] for p in report['provers']}
    render_cactus(curves, output, label, report['common_eligible'], budget, colors)


def make_scope(data, provers, output, label, baseline, budget, colors):
    output.mkdir(parents=True, exist_ok=True)
    report, eligible, solved = summarize(data, provers)
    fields = ['Prover', 'Solved', 'Solved %', 'Unique among selected', 'Median solved seconds',
              'Both with baseline', 'Only prover', 'Only baseline']
    rows = []
    n = len(eligible)
    for p in provers:
        times = [data[p][t].seconds for t in solved[p]]
        rows.append([p, len(times), f'{100 * len(times) / n:.2f}' if n else 'n/a',
                     report['only_this_prover'][p], f'{statistics.median(times):.6f}' if times else '',
                     len(solved[p] & solved[baseline]), len(solved[p] - solved[baseline]),
                     len(solved[baseline] - solved[p])])
    portfolio = portfolio_results(data, provers, eligible)
    portfolio_solved = {t for t, r in portfolio.items() if r.solved}
    portfolio_times = [portfolio[t].seconds for t in portfolio_solved]
    rows.append([PORTFOLIO, len(portfolio_solved),
                 f'{100 * len(portfolio_solved) / n:.2f}' if n else 'n/a', '',
                 f'{statistics.median(portfolio_times):.6f}' if portfolio_times else '',
                 len(solved[baseline]), len(portfolio_solved - solved[baseline]), 0])
    report['portfolio'] = dict(components=provers, proved=len(portfolio_solved),
                              additional_vs_baseline=len(portfolio_solved - solved[baseline]))
    plot_data = {**data, PORTFOLIO: portfolio}
    plot_solved = {**solved, PORTFOLIO: portfolio_solved}
    plot_report = {**report, 'provers': [*provers, PORTFOLIO]}
    plot_colors = {**colors, PORTFOLIO: '#000000'}
    write_csv(output / 'comparison.csv', fields, rows)
    table = markdown_table(fields, rows)
    (output / 'comparison.md').write_text(
        f'# {label}\n\nBaseline: {baseline}. Common eligible: {n:,}.\n\n{table}\n')
    (output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    write_csv(output / 'cactus-data.csv', ['prover', 'solved_rank', 'seconds'],
              ((p, i, seconds) for p in plot_report['provers'] for i, seconds in enumerate(
                  sorted(plot_data[p][t].seconds for t in plot_solved[p]), 1)))
    plot_cactus(plot_data, plot_report, plot_solved, output, label, budget, plot_colors)
    plot_scatter([data], provers, baseline, eligible, output, budget, colors)
    return report, table


def check_collected(source, data, report, issues):
    for key in ('provers', 'common_completed', 'common_eligible', 'incomplete', 'excluded',
                'proved', 'proved_by_any', 'proved_by_all', 'only_this_prover', 'pairwise'):
        if source.get(key) != report[key]:
            issues.append(f'Collected JSON disagrees with XML: {key}')
    for pair in source.get('pairwise_comparisons', []):
        actual, _, _ = summarize(data, pair['provers'])
        for key in actual:
            if actual[key] != pair.get(key):
                issues.append(f'Collected pairwise JSON disagrees: {pair["provers"]}: {key}')


def check_tsv(path, data, eligible, issues):
    seen = set()
    mismatches = 0
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        for row in reader:
            t = row['theorem']
            if t in seen:
                mismatches += 1
            seen.add(t)
            if row['eligible'] != str(t in eligible) or any(
                    t not in data[p] or row.get(p) != data[p][t].status for p in data):
                mismatches += 1
    common = set.intersection(*(set(rows) for rows in data.values()))
    if seen != common or mismatches:
        issues.append(f'Collected TSV disagrees with XML: {mismatches} mismatches; '
                      f'{len(seen ^ common)} unmatched theorems')


def load_comparison(results, selected_budget=None):
    candidates = sorted(results.glob('comparison-search*.json'))
    if selected_budget is not None:
        candidates = [p for p in candidates if json.loads(p.read_text())['search_budget_seconds'] == selected_budget]
    if len(candidates) != 1:
        raise ValueError(f'{results}: expected one comparison-search*.json; select a budget with --budget')
    source_path = candidates[0]
    source = json.loads(source_path.read_text())
    budget = source['search_budget_seconds']
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError('Search budget must be positive and finite')
    suffix = source_path.stem.removeprefix('comparison-')
    data, diagnostics = {}, {}
    issues = []
    for expected in source['provers']:
        path = results / f'vampire-hammer.{expected}-{suffix}.results.xml'
        print(f'Reading {path.name}', flush=True)
        prover, rows, audit = read_results(path, budget)
        if prover != expected:
            raise ValueError(f'{path}: expected prover {expected}, got {prover}')
        data[prover], diagnostics[prover] = rows, audit
        issues.extend(f'{prover}: {k}: {v}' for k, v in audit['issues'].items())
        if audit.get('metadata', {}).get('theorem_mismatch'):
            issues.append(f'{prover}: {audit["metadata"]["theorem_mismatch"]} reported theorem(s) '
                          'disagree with the scheduled task; theorem identities use scheduled arguments')
    report, eligible, _ = summarize(data, list(data))
    print('Checking collected JSON and TSV...', flush=True)
    check_collected(source, data, report, issues)
    tsv = source_path.with_suffix('.tsv')
    if tsv.exists():
        check_tsv(tsv, data, eligible, issues)
    else:
        issues.append('Collected TSV is absent; cross-check skipped')
    if report['incomplete']:
        issues.append(f'{report["incomplete"]} theorem(s) missing one or more tool results')
    return data, diagnostics, budget, issues


def matched_runs(runs, provers):
    """Use the same completed, eligible population in every repetition."""
    summaries = [summarize(run, provers) for run in runs]
    tasks = set.intersection(*(item[1] for item in summaries))
    solved = [{p: item[2][p] & tasks for p in provers} for item in summaries]
    coverage = [dict(common_completed=item[0]['common_completed'],
                     common_eligible=item[0]['common_eligible'],
                     removed_for_matching=len(item[1] - tasks)) for item in summaries]
    return tasks, solved, coverage


def sample_stats(values):
    return dict(mean=statistics.mean(values),
                stdev=statistics.stdev(values) if len(values) > 1 else None,
                min=min(values), max=max(values), per_run=values)


def mean_sd(values):
    s = sample_stats(values)
    return f'{s["mean"]:,.1f} ± {s["stdev"]:,.1f}' if s['stdev'] is not None else f'{s["mean"]:,.1f}'


def average_curve(run_times):
    """Mean solved count at each observed time; failures never become successes."""
    times = sorted(t for times in run_times for t in times)
    repetitions = len(run_times)
    return [(i / repetitions, t) for i, t in enumerate(times, 1)]


def plot_average(curves, output, label, n, budget, colors):
    render_cactus(curves, output, label, n, budget, colors)


def average_scope(runs, provers, baseline, output, label, budget, colors):
    output.mkdir(parents=True, exist_ok=True)
    tasks, solved, coverage = matched_runs(runs, provers)
    # Add the virtual tool only after matching eligibility. Its successes must
    # not participate in the real tools' unique-success calculations.
    runs = [{**run, PORTFOLIO: portfolio_results(run, provers, tasks)} for run in runs]
    for s in solved:
        s[PORTFOLIO] = set.union(*(s[p] for p in provers))
    fields = ['Prover', *[f'Run {i + 1} solved' for i in range(len(runs))],
              'Mean solved ± sample SD', 'Mean solved %', 'Solved every run', 'Solved any run',
              'Mean unique ± SD', 'Mean both with baseline', 'Mean only prover', 'Mean only baseline']
    rows, details, curves = [], {}, {}
    count_fields = ['prover', 'eligible', *[f'run_{i + 1}_solved' for i in range(len(runs))],
                    'mean_solved', 'sample_sd_solved', 'min_solved', 'max_solved',
                    'mean_solved_percent', 'solved_every_run', 'solved_any_run',
                    'mean_unique', 'sample_sd_unique', 'mean_both_with_baseline',
                    'mean_only_prover', 'mean_only_baseline']
    numeric_rows = []
    for p in [*provers, PORTFOLIO]:
        sets = [s[p] for s in solved]
        counts = list(map(len, sets))
        stats = sample_stats(counts)
        always, ever = set.intersection(*sets), set.union(*sets)
        unique = ([len(s[p] - set().union(*(s[q] for q in provers if q != p))) for s in solved]
                  if p != PORTFOLIO else None)
        both = [len(s[p] & s[baseline]) for s in solved]
        only_p = [len(s[p] - s[baseline]) for s in solved]
        only_b = [len(s[baseline] - s[p]) for s in solved]
        percent = 100 * stats['mean'] / len(tasks) if tasks else None
        rows.append([p, *counts, mean_sd(counts), f'{percent:.2f}' if percent is not None else 'n/a',
                     len(always), len(ever), mean_sd(unique) if unique is not None else '', f'{statistics.mean(both):,.1f}',
                     f'{statistics.mean(only_p):,.1f}', f'{statistics.mean(only_b):,.1f}'])
        numeric_rows.append([p, len(tasks), *counts, stats['mean'], stats['stdev'], stats['min'], stats['max'],
                             percent, len(always), len(ever), statistics.mean(unique) if unique is not None else None,
                             sample_stats(unique)['stdev'] if unique is not None else None, statistics.mean(both),
                             statistics.mean(only_p), statistics.mean(only_b)])
        details[p] = dict(solved=stats, unique=sample_stats(unique) if unique is not None else None, solved_every_run=len(always),
                          solved_any_run=len(ever), both_with_baseline=sample_stats(both),
                          only_prover=sample_stats(only_p), only_baseline=sample_stats(only_b))
        if p == PORTFOLIO:
            details[p].update(components=provers, theoretical=True)
        curves[p] = [sorted(run[p][t].seconds for t in s[p]) for run, s in zip(runs, solved)]
        def problem_rows():
            for t in sorted(tasks):
                records = [run[p][t] for run in runs]
                times = [r.seconds for r in records if r.solved]
                yield [t, *[r.status for r in records], *[r.seconds for r in records],
                       len(times), len(times) / len(runs), statistics.mean(times) if times else None]
        write_csv(output / f'problems-{p}.csv',
                  ['theorem', *[f'run_{i + 1}_status' for i in range(len(runs))],
                   *[f'run_{i + 1}_seconds' for i in range(len(runs))],
                   'successful_runs', 'success_fraction', 'mean_seconds_successful_runs_only'], problem_rows())
    write_csv(output / 'comparison.csv', count_fields, numeric_rows)
    write_csv(output / 'cactus-data.csv', ['prover', 'mean_solved', 'seconds'],
              ((p, x, t) for p, times in curves.items() for x, t in average_curve(times)))
    summary = dict(common_eligible=len(tasks), repetitions=len(runs), coverage=coverage, provers=details)
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    table = markdown_table(fields, rows)
    (output / 'comparison.md').write_text(f'# {label}\n\n{len(tasks):,} matched eligible theorems.\n\n{table}\n')
    plot_average(curves, output, label, len(tasks), budget, {**colors, PORTFOLIO: '#000000'})
    plot_scatter(runs, provers, baseline, tasks, output, budget, colors)
    return summary, fields, rows


def average_runs(args):
    paths = [path.resolve() for path in args.results]
    if len(set(paths)) != len(paths):
        raise ValueError('Each repetition must be a different result directory')
    # Validate every path before reading several gigabytes from the first run.
    for path in paths:
        if not path.is_dir():
            raise ValueError(f'Result directory does not exist: {path}')
    output = args.output.expanduser()
    output.mkdir(parents=True, exist_ok=True)
    runs, audits, issues, budget = [], [], [], None
    for i, path in enumerate(paths, 1):
        print(f'Loading run {i}: {path}', flush=True)
        data, diagnostics, current_budget, current_issues = load_comparison(path, args.budget)
        if budget is not None and current_budget != budget:
            raise ValueError('Runs have different proof budgets; averaging them would be misleading')
        if runs and set(data) != set(runs[0]):
            raise ValueError('Runs contain different prover sets')
        if args.baseline not in data:
            raise ValueError(f'Baseline {args.baseline} is absent')
        budget = current_budget
        audits.append(dict(path=str(path), issues=current_issues, provers=diagnostics))
        issues.extend(f'Run {i}: {issue}' for issue in current_issues)
        (output / f'audit-run-{i}.json').write_text(json.dumps(audits[-1], indent=2) + '\n')
        if any(a['issues'] for a in diagnostics.values()):
            raise ValueError(f'Run {i} has invalid records; see audit-run-{i}.json')
        runs.append(data)
    provers = list(runs[0])
    palette = list(TOOL_COLORS.values())
    colors = {p: TOOL_COLORS.get(p, palette[i % len(palette)]) for i, p in enumerate(provers)}
    scopes = [('common-all', provers, 'All tools')]
    if 'lean-smt' in provers and len(provers) > 2 and args.baseline != 'lean-smt':
        scopes.append(('common-without-lean-smt', [p for p in provers if p != 'lean-smt'],
                       'Tools excluding Lean-SMT'))
    explanation = (f'{len(runs)} runs; {budget:g}s proof budget; baseline {args.baseline}. '
                   'Every scope uses the same theorems, completed and eligible for all selected tools '
                   'in every run. Counts are arithmetic means across runs; ± is sample standard '
                   'deviation, not a confidence interval. Unique and pairwise counts are computed '
                   'within each run before averaging. Bold cactus curves show the mean number '
                   'solved by each time threshold; faint dashed curves show individual runs. '
                   'Theoretical-best combines all selected tools within each run: a theorem counts '
                   'once if any tool solves it, at the fastest successful time. This is an ideal '
                   'parallel portfolio with one worker per tool and no overhead. Its union is '
                   'computed within each run before averaging. Blank unique counts are not applicable '
                   'to this virtual portfolio; real-tool unique counts exclude the virtual tool. '
                   'Failures do not contribute successful timings. Per-problem mean times are '
                   'explicitly conditional on successful runs. Lin-log plots use a linear solved-count axis and a logarithmic proof-time axis in seconds; zero times are displayed at one microsecond. Tool names use small caps, while Theoretical best uses the normal serif font; a scriptsize table beside the cactus plot reports solved and all-solver unique counts. '
                   'Original suite versions and machine settings are not '
                   'independently verified by this report.')
    sections = ['# Averaged benchmark comparison\n\n' + explanation,
                'Inputs:\n\n' + '\n'.join(f'- Run {i}: `{p}`' for i, p in enumerate(paths, 1))]
    tables = []
    scope_summaries = {}
    for slug, selected, label in scopes:
        print(f'Averaging and plotting {label}...', flush=True)
        summary, fields, rows = average_scope(runs, selected, args.baseline, output / slug,
                                              label, budget, colors)
        scope_summaries[slug] = summary
        sections.append(f'## {label}\n\n{summary["common_eligible"]:,} matched eligible theorems.\n\n' +
                        markdown_table(fields, rows))
        tables.append((slug, label, fields, rows))
        for i, coverage in enumerate(summary['coverage'], 1):
            if coverage['removed_for_matching']:
                issues.append(f'{label}, run {i}: {coverage["removed_for_matching"]} '
                              'eligible theorems omitted to match the other runs')
    pair_fields = ['Prover', 'Matched eligible pair', 'Mean baseline solved ± SD', 'Mean prover solved ± SD',
                   'Mean both ± SD', 'Mean only baseline ± SD', 'Mean only prover ± SD', 'Mean neither ± SD']
    pair_rows, pair_details = [], {}
    pairs_dir = output / 'vs-baseline'
    pairs_dir.mkdir(exist_ok=True)
    for p in provers:
        if p == args.baseline:
            continue
        tasks, solved, coverage = matched_runs(runs, [args.baseline, p])
        metrics = defaultdict(list)
        for s in solved:
            a, b = s[args.baseline], s[p]
            for key, value in dict(baseline_solved=len(a), prover_solved=len(b), both=len(a & b),
                                   only_baseline=len(a - b), only_prover=len(b - a),
                                   neither=len(tasks - (a | b))).items():
                metrics[key].append(value)
        pair_rows.append([p, len(tasks), *[mean_sd(v) for v in metrics.values()]])
        pair_details[p] = dict(common_eligible=len(tasks), coverage=coverage,
                              metrics={key: sample_stats(value) for key, value in metrics.items()})
        for i, c in enumerate(coverage, 1):
            if c['removed_for_matching']:
                issues.append(f'Pair {p}, run {i}: {c["removed_for_matching"]} eligible theorems omitted for matching')
        def pair_problem_rows():
            for t in sorted(tasks):
                a = [t in s[args.baseline] for s in solved]
                b = [t in s[p] for s in solved]
                yield [t, sum(a), sum(b), sum(x and y for x, y in zip(a, b)),
                       sum(x and not y for x, y in zip(a, b)), sum(y and not x for x, y in zip(a, b)),
                       *[run[args.baseline][t].status for run in runs], *[run[p][t].status for run in runs]]
        write_csv(pairs_dir / f'{p}.csv',
                  ['theorem', 'baseline_successful_runs', 'prover_successful_runs', 'both_runs',
                   'only_baseline_runs', 'only_prover_runs',
                   *[f'run_{i + 1}_baseline_status' for i in range(len(runs))],
                   *[f'run_{i + 1}_prover_status' for i in range(len(runs))]], pair_problem_rows())
    write_csv(output / 'pairwise-vs-baseline.csv', pair_fields, pair_rows)
    (output / 'summary.json').write_text(json.dumps(dict(inputs=list(map(str, paths)), budget=budget,
                 repetitions=len(runs), scopes=scope_summaries, pairwise=pair_details, issues=issues), indent=2) + '\n')
    sections.append('## Compact cactus figures\n\n' + CACTUS_CAPTION)
    sections.append('## Proof-time scatter plots\n\n' + SCATTER_CAPTION)
    sections.append('## Pairwise against baseline\n\nEach pair uses its own matched eligible set across all runs.\n\n' +
                    markdown_table(pair_fields, pair_rows))
    sections.append('## Data checks\n\n' + ('\n'.join(f'- {x}' for x in issues) if issues else
                    'Collected summaries and recorded success checks agree for each input run.'))
    (output / 'report.md').write_text('\n\n'.join(sections) + '\n')
    def html_table(fields, rows):
        return '<table><tr>' + ''.join(f'<th>{html.escape(str(v))}</th>' for v in fields) + '</tr>' + ''.join(
            '<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in row) + '</tr>' for row in rows) + '</table>'
    page = ['<!doctype html><meta charset="utf-8"><title>Averaged benchmark comparison</title>',
            '<style>body{font:15px system-ui;margin:2em;padding:1em}table{border-collapse:collapse}'
            'td,th{border:1px solid #ccc;padding:.5em;text-align:right}img{max-width:1000px;width:100%}'
            'pre{white-space:pre-wrap}.scroll{overflow-x:auto}</style>',
            '<h1>Averaged benchmark comparison</h1><p>' + html.escape(explanation) + '</p>',
            '<pre>' + html.escape(sections[1]) + '</pre>']
    if issues:
        page.append('<p><strong>PROVISIONAL: review the data checks below.</strong></p>')
    for slug, label, fields, rows in tables:
        page.extend([f'<h2>{html.escape(label)}</h2><div class="scroll">', html_table(fields, rows), '</div>',
                     f'<img src="{slug}/cactus-linlog.svg" alt="Cactus plot with solved and exclusive counts">',
                     '<p>' + html.escape(CACTUS_CAPTION) + '</p>',
                     '<p>' + html.escape(SCATTER_CAPTION) + '</p>',
                     f'<img src="{slug}/scatter-vs-baseline.svg" alt="Paired proof-time scatter plots">'])
    page.extend(['<h2>Pairwise against baseline</h2><div class="scroll">', html_table(pair_fields, pair_rows),
                 '</div><h2>Data checks</h2><pre>' + html.escape(sections[-1]) + '</pre>'])
    (output / 'index.html').write_text('\n'.join(page))
    print(f'Averaged report: {output.resolve() / "index.html"}\nAudit issues: {len(issues)}', flush=True)
    return 1 if issues else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', nargs='+', type=lambda p: Path(p).expanduser(),
                        help='One or more independent result directories; multiple runs are averaged')
    parser.add_argument('--output', type=Path, default=Path('artifacts/comparison'))
    parser.add_argument('--budget', type=float, help='Select comparison search budget (seconds)')
    parser.add_argument('--baseline', default='vampire-hammer')
    args = parser.parse_args()
    if len(args.results) > 1:
        return average_runs(args)
    data, diagnostics, budget, issues = load_comparison(args.results[0], args.budget)
    if args.baseline not in data:
        parser.error(f'Baseline {args.baseline} is absent')
    output = args.output.expanduser()
    output.mkdir(parents=True, exist_ok=True)
    # Fixed colors make the same tool identifiable across all scopes.
    palette = list(TOOL_COLORS.values())
    colors = {p: TOOL_COLORS.get(p, palette[i % len(palette)]) for i, p in enumerate(data)}
    if any(a['issues'] for a in diagnostics.values()):
        # Never silently drop a successful result from the timing plots.
        (output / 'audit.json').write_text(json.dumps(dict(issues=issues, provers=diagnostics), indent=2))
        raise ValueError(f'Invalid records; see {output / "audit.json"}')
    scopes = [('common-all', list(data), 'All tools')]
    if 'lean-smt' in data and len(data) > 2 and args.baseline != 'lean-smt':
        scopes.append(('common-without-lean-smt', [p for p in data if p != 'lean-smt'],
                       'Tools excluding Lean-SMT'))
    sections = [f'# Benchmark comparison\n\nProof budget: {budget:g}s. Baseline: {args.baseline}. '
                'Only status=true results count as solved. Cactus curves use sorted per-theorem '
                'hammer-seconds, including proof checking; CPU/RSS and batch startup are not plotted. '
                'Errors remain unsolved in the eligible denominator. Each scope excludes any theorem '
                'excluded by one of its tools. Unique counts refer to the selected tools. '
                'Lin-log plots use a linear solved-count axis and a logarithmic proof-time axis in seconds; zero times are displayed at one microsecond. Tool names use small caps, while Theoretical best uses the normal serif font; a scriptsize table beside the cactus plot reports solved and all-solver unique counts.']
    sections[0] += (' Theoretical-best is the union of the selected tools, with the fastest '
                    'successful time per theorem: an ideal parallel portfolio with one worker '
                    'per tool and no overhead. Its blank unique count is not applicable; '
                    'real-tool unique counts exclude the virtual portfolio.')
    links = []
    for slug, selected, label in scopes:
        print(f'Plotting {label}...', flush=True)
        summary, table = make_scope(data, selected, output / slug, label, args.baseline, budget, colors)
        sections.append(f'## {label}\n\n{summary["common_eligible"]:,} common eligible; '
                        f'{summary["excluded"]:,} excluded; {summary["incomplete"]:,} incomplete.\n\n{table}')
        links.append((slug, label, table))
    pair_fields = ['Prover', 'Eligible pair', 'Baseline solved', 'Prover solved', 'Both',
                   'Only baseline', 'Only prover', 'Neither', 'Net baseline advantage']
    pair_rows = []
    pairs_dir = output / 'vs-baseline'
    pairs_dir.mkdir(exist_ok=True)
    for p in data:
        if p == args.baseline:
            continue
        pair, tasks, solved = summarize(data, [args.baseline, p])
        a, b = solved[args.baseline], solved[p]
        pair_rows.append([p, len(tasks), len(a), len(b), len(a & b), len(a - b), len(b - a),
                          len(tasks - (a | b)), len(a) - len(b)])
        write_csv(pairs_dir / f'{p}.csv',
                  ['theorem', 'relation', 'baseline_status', 'prover_status',
                   'baseline_seconds', 'prover_seconds', 'eligible_in_common_all'],
                  ((t, 'both' if t in a and t in b else 'only_baseline' if t in a else
                    'only_prover' if t in b else 'neither',
                    data[args.baseline][t].status, data[p][t].status,
                    data[args.baseline][t].seconds, data[p][t].seconds, t in eligible)
                   for t in sorted(tasks)))
        for name, tasks_subset in [('only-baseline', a - b), ('only-prover', b - a)]:
            write_csv(pairs_dir / f'{p}-{name}.csv', ['theorem'], ((t,) for t in sorted(tasks_subset)))
    write_csv(output / 'pairwise-vs-baseline.csv', pair_fields, pair_rows)
    sections.append('## Compact cactus figures\n\n' + CACTUS_CAPTION)
    sections.append('## Proof-time scatter plots\n\n' + SCATTER_CAPTION)
    sections.append('## Pairwise against baseline\n\nEach row uses its own pair of tools’ common '
                    'eligible set; denominators can differ. Per-theorem outcomes and exclusive '
                    'theorem lists are in `vs-baseline/`.\n\n' + markdown_table(pair_fields, pair_rows))
    audit_rows = []
    for p, audit in diagnostics.items():
        statuses = audit['eligible_status']
        errors = sum(v for k, v in statuses.items() if k.startswith('ERROR'))
        audit_rows.append([p, audit['rows'], sum(statuses.values()), errors,
                           statuses.get('TIMEOUT', 0),
                           json.dumps(audit['category'], ensure_ascii=False)])
    audit_fields = ['Prover', 'Rows', 'Individually eligible', 'ERROR statuses', 'TIMEOUT statuses', 'Categories (all rows)']
    write_csv(output / 'audit-counts.csv', audit_fields, audit_rows)
    sections.append('## Data checks\n\n' + ('\n'.join(f'- {i}' for i in issues) if issues else
                    'XML counts, success outcomes, kernel-check flags, proof times, collected JSON '
                    '(including pairwise summaries), and TSV agree. No duplicate theorem rows.') +
                    '\n\nError statuses can include tactic failures and rejected proofs; they do not '
                    'by themselves indicate a broken job. Inspect audit.json for categories, messages '
                    'and examples. The source journals/log directories are not recreated by this script. '
                    'Completeness is checked against the collected files, not the original suite catalog. '
                    'If task/theorem mismatches are reported, plots and tables are provisional: '
                    'a row may contain another theorem’s result.\n\n' +
                    markdown_table(audit_fields, audit_rows))
    (output / 'audit.json').write_text(json.dumps(dict(issues=issues, provers=diagnostics), indent=2) + '\n')
    (output / 'report.md').write_text('\n\n'.join(sections) + '\n')
    # A self-contained table/report page; plots are adjacent SVG files.
    def html_table(fields, rows):
        return '<table><tr>' + ''.join(f'<th>{html.escape(str(x))}</th>' for x in fields) + '</tr>' + ''.join(
            '<tr>' + ''.join(f'<td>{html.escape(str(x))}</td>' for x in row) + '</tr>' for row in rows) + '</table>'
    page = ['<!doctype html><meta charset="utf-8"><title>Benchmark comparison</title>',
            '<style>body{font:15px system-ui;max-width:1200px;margin:2em auto;padding:1em}'
            'table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.5em;text-align:right}'
            'img{max-width:100%}pre{white-space:pre-wrap}</style><h1>Benchmark comparison</h1>',
            f'<p>Proof budget: {budget:g}s. Baseline: {html.escape(args.baseline)}. '
            'Tables use the common eligible set in each scope. Errors count as unsolved. '
            'Proof times use hammer-seconds. See report.md and audit.json for checks and details.</p>',
            '<p>Theoretical-best combines all selected tools, counting each solved theorem once '
            'at its fastest successful time: an ideal parallel portfolio with one worker per tool '
            'and no overhead. Its unique count is not applicable.</p>',
            '<p><strong>' + html.escape('PROVISIONAL: audit issues found. ' + '; '.join(issues)
                                       if issues else 'Data consistency checks passed.') + '</strong></p>']
    for slug, label, _ in links:
        with (output / slug / 'comparison.csv').open(newline='') as stream:
            contents = list(csv.reader(stream))
        page += [f'<h2>{html.escape(label)}</h2>', html_table(contents[0], contents[1:]),
                 f'<img src="{slug}/cactus-linlog.svg" alt="Cactus plot with solved and exclusive counts">',
                 '<p>' + html.escape(CACTUS_CAPTION) + '</p>',
                 f'<img src="{slug}/cactus-linear.svg" alt="Linear cactus plot">',
                 '<p>' + html.escape(SCATTER_CAPTION) + '</p>',
                 f'<img src="{slug}/scatter-vs-baseline.svg" alt="Paired proof-time scatter plots">']
    page += ['<h2>Pairwise against baseline</h2><p>Each row uses its own eligible pair.</p>',
             html_table(pair_fields, pair_rows), '<h2>Data checks</h2><pre>',
             html.escape(sections[-1]), '</pre>']
    (output / 'index.html').write_text('\n'.join(page))
    print(f'Report: {output.resolve() / "index.html"}\nAudit issues: {len(issues)}', flush=True)
    return 1 if issues else 0


if __name__ == '__main__':
    sys.exit(main())
