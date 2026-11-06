// Assemble the report .docx from report/sections/*.md, out/tables.json and out/fig/*.png.
//   node report/build.mjs <out_dir> <file.docx>
// The section files use a small Markdown subset: # / ## headings, paragraphs, "- " bullets,
// pipe tables, ```code``` blocks, **bold**, *italic*, `code`, and three directives:
//   {{fig:name|caption|width_cm}}   {{table:name}}   {{box:text}}
import { createRequire } from "node:module";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const d = require("docx");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell,
  WidthType, BorderStyle, ShadingType, ImageRun, Footer, PageNumber, LevelFormat, TabStopType,
  TabStopPosition, PageBreak, VerticalAlign, TableLayoutType, LineRuleType,
} = d;

const here = dirname(fileURLToPath(import.meta.url));
const [outDir, outFile] = process.argv.slice(2);
const TABLES = JSON.parse(readFileSync(join(outDir, "tables.json"), "utf8"));
const PAGES = JSON.parse(readFileSync(join(outDir, "index.json"), "utf8"));

const BODY = "Cambria", HEAD = "Calibri", MONO = "Consolas";
const TEXT_W = 9026; // A4 width 11906 - 2 x 1440 margins, in DXA
const RULE = { style: BorderStyle.SINGLE, size: 4, color: "9AA5B1" };
const BORDERS = { top: RULE, bottom: RULE, left: RULE, right: RULE, insideHorizontal: RULE, insideVertical: RULE };

// ------------------------------------------------------------------ inline text
function runs(text, base = {}) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const t = m[0];
    if (t.startsWith("**")) out.push(new TextRun({ text: t.slice(2, -2), bold: true, ...base }));
    else if (t.startsWith("`")) out.push(new TextRun({ text: t.slice(1, -1), font: MONO, size: (base.size || 24) - 3, ...base, ...{ font: MONO } }));
    else out.push(new TextRun({ text: t.slice(1, -1), italics: true, ...base }));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...base }));
  return out;
}

// ------------------------------------------------------------------ tables
// Zero-width spaces after "_" in long identifiers let Word wrap client_approved_at
// at an underscore instead of in the middle of a word.
function softBreaks(text) {
  return text.replace(/[A-Za-z0-9_]{13,}/g, (w) => w.replace(/_/g, "_\u200B"));
}

function widthsFor(rows, total, size) {
  // Every column is at least as wide as its longest word, so names never break
  // mid-word; the remaining width goes to the columns with the most text.
  const n = rows[0].length;
  const charW = size * 6.5;                       // ≈ DXA per character (capitals) at this size
  const words = (c) => rows.flatMap((r) => String(r[c] ?? "").split(/[\s\n]+/));
  let min = Array.from({ length: n }, (_, c) => Math.min(Math.max(...words(c).map((w) => w.length)), 16) * charW + 260);
  const minSum = min.reduce((a, b) => a + b, 0);
  if (minSum > total) {
    // keep short columns (like "BCNF") whole; squeeze only the wide ones
    const small = min.map((m) => m <= total / n);
    const fixed = min.reduce((a, m, c) => a + (small[c] ? m : 0), 0);
    const wide = minSum - fixed;
    min = min.map((m, c) => (small[c] ? m : (m / wide) * (total - fixed)));
  }
  const textLen = Array.from({ length: n }, (_, c) =>
    Math.max(...rows.map((r) => String(r[c] ?? "").length)));
  const spare = total - min.reduce((a, b) => a + b, 0);
  const extra = textLen.map((l, c) => Math.max(0, l * charW - min[c]));
  const extraSum = extra.reduce((a, b) => a + b, 0) || 1;
  const w = min.map((m, c) => Math.floor(m + (spare * extra[c]) / extraSum));
  w[w.length - 1] += total - w.reduce((a, b) => a + b, 0);
  return w;
}

function table(rows, { size = 18, header = true } = {}) {
  const many = rows[0].length > 8;
  if (many) size = 15;
  const widths = widthsFor(rows, TEXT_W, size);
  const keep = rows.length <= 12;   // small tables move to the next page whole
  return new Table({
    width: { size: TEXT_W, type: WidthType.DXA },
    columnWidths: widths,
    layout: TableLayoutType.FIXED,
    borders: BORDERS,
    rows: rows.map((r, i) => new TableRow({
      tableHeader: header && i === 0,
      cantSplit: true,
      children: r.map((cell, c) => new TableCell({
        width: { size: widths[c], type: WidthType.DXA },
        shading: header && i === 0 ? { type: ShadingType.CLEAR, fill: "E8EEF4", color: "auto" } : undefined,
        margins: { top: 50, bottom: 50, left: many ? 40 : 90, right: many ? 40 : 90 },
        verticalAlign: VerticalAlign.TOP,
        children: String(cell ?? "").split("\n").map((line) => softBreaks(line)).map((line) => new Paragraph({
          spacing: { before: 0, after: 0, line: 240 },
          keepNext: (keep && i < rows.length - 1) || i < 2,
          alignment: many && c > 0 ? AlignmentType.CENTER : AlignmentType.LEFT,
          children: runs(line, { size, font: header && i === 0 ? HEAD : BODY, bold: header && i === 0 }),
        })),
      })),
    })),
  });
}

function spacer(after = 120) {
  return new Paragraph({ spacing: { before: 0, after }, children: [] });
}

// ------------------------------------------------------------------ figures
let figNo = 0, curSection = 0;
function pngSize(path) {
  const b = readFileSync(path);
  return [b.readUInt32BE(16), b.readUInt32BE(20)];
}
function figure(name, caption, widthCm) {
  const path = join(outDir, "fig", `${name}.png`);
  const [w, h] = pngSize(path);
  let width = (Number(widthCm) || 16) / 2.54 * 96;
  let height = width * (h / w);
  const maxH = 22 / 2.54 * 96;
  if (height > maxH) { width *= maxH / height; height = maxH; }
  figNo += 1;
  return [
    new Paragraph({ alignment: AlignmentType.CENTER, keepNext: true,
      spacing: { before: 120, after: 60, line: 240, lineRule: LineRuleType.AUTO },
      children: [new ImageRun({ type: "png", data: readFileSync(path),
        transformation: { width: Math.round(width), height: Math.round(height) },
        altText: { title: caption, description: caption, name } })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
      children: [new TextRun({ text: `Figure ${curSection}.${figNo}: `, bold: true, size: 20 }),
                 new TextRun({ text: caption, italics: true, size: 20 })] }),
  ];
}

function box(text) {
  const dash = { style: BorderStyle.DASHED, size: 8, color: "7A8794" };
  return new Table({
    width: { size: TEXT_W, type: WidthType.DXA }, columnWidths: [TEXT_W],
    borders: { top: dash, bottom: dash, left: dash, right: dash },
    rows: [new TableRow({ height: { value: 2600, rule: "atLeast" }, children: [new TableCell({
      width: { size: TEXT_W, type: WidthType.DXA }, verticalAlign: VerticalAlign.CENTER,
      shading: { type: ShadingType.CLEAR, fill: "F6F7F8", color: "auto" },
      children: [new Paragraph({ alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: text, italics: true, color: "5A6470", size: 22 })] })],
    })] })],
  });
}

// ------------------------------------------------------------------ section parser
function parse(md) {
  const out = [];
  const lines = md.replace(/\r/g, "").split("\n");
  let i = 0, para = [];
  const flush = () => {
    if (para.length) {
      out.push(new Paragraph({ alignment: AlignmentType.JUSTIFIED, spacing: { after: 140 },
                               children: runs(para.join(" ")) }));
      para = [];
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^# /.test(line)) {
      flush();
      const text = line.slice(2).trim();
      const m = text.match(/^(\d+)\./);
      curSection = m ? Number(m[1]) : curSection;
      figNo = 0;
      out.push(new Paragraph({ heading: HeadingLevel.HEADING_1, pageBreakBefore: true, children: [new TextRun(text)] }));
    } else if (/^## /.test(line)) {
      flush();
      out.push(new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(line.slice(3).trim())] }));
    } else if (/^```/.test(line)) {
      flush();
      const code = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      code.forEach((c, k) => out.push(new Paragraph({
        spacing: { before: k === 0 ? 60 : 0, after: k === code.length - 1 ? 160 : 0, line: 240 },
        shading: { type: ShadingType.CLEAR, fill: "F3F4F6", color: "auto" },
        keepLines: true, keepNext: k < code.length - 1,
        children: [new TextRun({ text: c || " ", font: MONO, size: 17 })] })));
    } else if (/^\{\{fig:/.test(line)) {
      flush();
      const [name, caption, w] = line.slice(6, -2).split("|");
      out.push(...figure(name, caption, w));
    } else if (/^\{\{table:/.test(line)) {
      flush();
      const name = line.slice(8, -2);
      if (!TABLES[name]) throw new Error(`unknown table ${name}`);
      out.push(table(TABLES[name]), spacer(160));
    } else if (/^\{\{box:/.test(line)) {
      flush();
      out.push(box(line.slice(6, -2)), spacer(200));
    } else if (/^\|/.test(line)) {
      flush();
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map((s) => s.trim());
        if (!cells.every((c) => /^:?-{3,}:?$/.test(c))) rows.push(cells);
        i += 1;
      }
      out.push(table(rows), spacer(160));
      continue;
    } else if (/^- /.test(line)) {
      flush();
      let text = line.slice(2);
      while (i + 1 < lines.length && /^  \S/.test(lines[i + 1])) text += " " + lines[++i].trim();
      out.push(new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 60 },
                               alignment: AlignmentType.LEFT, children: runs(text) }));
    } else if (line.trim() === "") {
      flush();
    } else {
      para.push(line.trim());
    }
    i += 1;
  }
  flush();
  return out;
}

// ------------------------------------------------------------------ cover and index
function coverPage() {
  const c = (text, opts = {}) => new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: opts.after ?? 120 },
    children: [new TextRun({ text, font: opts.font || HEAD, size: opts.size || 24, bold: opts.bold, italics: opts.italics,
                             highlight: opts.hl ? "yellow" : undefined })] });
  const people = [["Name", "Roll No."], ...Array.from({ length: 4 }, () => ["[Name]", "[Roll No.]"])];
  const w = [4513, 4513];
  const grid = new Table({
    width: { size: TEXT_W, type: WidthType.DXA }, columnWidths: w, borders: BORDERS,
    alignment: AlignmentType.CENTER,
    rows: people.map((r, i) => new TableRow({ children: r.map((cell, k) => new TableCell({
      width: { size: w[k], type: WidthType.DXA },
      shading: i === 0 ? { type: ShadingType.CLEAR, fill: "E8EEF4", color: "auto" } : undefined,
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: cell,
        font: i === 0 ? HEAD : BODY, bold: i === 0, size: 24, highlight: i > 0 ? "yellow" : undefined })] })] })) })),
  });
  return [
    spacer(600),
    c("Netaji Subhas University of Technology", { size: 32, bold: true, after: 80 }),
    c("[Department]", { size: 24, hl: true, after: 600 }),
    c("Database Management Systems Lab Project", { size: 26, after: 120 }),
    c("[Course code]", { size: 24, hl: true, after: 900 }),
    c("Reconcile", { size: 56, bold: true, after: 120 }),
    c("GST Input Tax Credit Reconciliation for CA Firms", { size: 30, after: 900 }),
    c("Submitted by", { size: 24, bold: true, after: 160 }),
    grid,
    spacer(600),
    c("Submitted to", { size: 24, bold: true, after: 80 }),
    c("[Supervisor]", { size: 24, hl: true, after: 600 }),
    c("[Month Year]", { size: 24, hl: true, after: 0 }),
  ];
}

function indexPage(headings) {
  const out = [new Paragraph({ alignment: AlignmentType.LEFT, spacing: { after: 360 },
    children: [new TextRun({ text: "Index", font: HEAD, size: 40, bold: true })] })];
  for (const h of headings) {
    const page = PAGES[h] ? String(PAGES[h]) : "";
    out.push(new Paragraph({
      spacing: { after: 160 },
      tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX, leader: "dot" }],
      children: [new TextRun({ text: h, font: BODY, size: 24 }), new TextRun({ text: `\t${page}`, font: BODY, size: 24 })],
    }));
  }
  return out;
}

// ------------------------------------------------------------------ assemble
const files = readdirSync(join(here, "sections")).filter((f) => f.endsWith(".md")).sort();
const mds = files.map((f) => readFileSync(join(here, "sections", f), "utf8"));
const headings = mds.flatMap((md) => [...md.matchAll(/^# (.*)$/gm)].map((m) => m[1].trim()));
const body = mds.flatMap(parse);
// The first chapter follows the index's own page break, so it must not add another.
body[0] = new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(headings[0])] });

const doc = new Document({
  creator: "Reconcile project group",
  title: "Reconcile: GST Input Tax Credit Reconciliation for CA Firms",
  styles: {
    default: { document: { run: { font: BODY, size: 24 }, paragraph: { spacing: { line: 276, lineRule: LineRuleType.AUTO } } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: HEAD, size: 34, bold: true, color: "1D2838" },
        paragraph: { spacing: { before: 0, after: 240 }, keepNext: true, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: HEAD, size: 26, bold: true, color: "1D2838" },
        paragraph: { spacing: { before: 280, after: 120 }, keepNext: true, outlineLevel: 1 } },
    ],
  },
  numbering: { config: [{ reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
    alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } } } }] }] },
  sections: [
    { properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 } } },
      children: [...coverPage(), new Paragraph({ children: [new PageBreak()] }), ...indexPage(headings)] },
    { properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1440, bottom: 1300, left: 1440, right: 1440 },
                            pageNumbers: { start: 1 } } },
      footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
        children: [new TextRun({ children: [PageNumber.CURRENT], size: 20, font: BODY })] })] }) },
      children: body },
  ],
});

writeFileSync(outFile, await Packer.toBuffer(doc));
console.log(`wrote ${outFile}`);
