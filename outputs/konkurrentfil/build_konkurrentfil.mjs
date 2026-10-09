import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "/Users/anderskinch/hostel";
const sourcePath = path.join(root, "samples", "konkurrentpriser_kilder_2026-10-03.csv");
const outputDir = path.join(root, "outputs", "konkurrentfil");
const xlsxPath = path.join(outputDir, "konkurrentpriser_forbedret_2026-10-03.xlsx");
const csvPath = path.join(root, "samples", "konkurrentpriser_forbedret_2026-10-03.csv");

const font = "Arial";
const palette = {
  navy: "#17405C",
  blue: "#2F6F97",
  lightBlue: "#E7F0F6",
  green: "#2E7D62",
  amber: "#F6C85F",
  red: "#C94C4C",
  gray: "#E7EAEE",
  text: "#1F2933",
  muted: "#667085",
};

function parseSemicolonCsv(text) {
  const lines = text.trim().split(/\r?\n/);
  const headers = lines[0].split(";");
  return lines.slice(1).filter(Boolean).map((line) => {
    const cells = line.split(";");
    const row = {};
    headers.forEach((h, i) => {
      row[h] = cells[i] ?? "";
    });
    return row;
  });
}

function num(value) {
  if (value === undefined || value === null || value === "") return null;
  const n = Number(String(value).replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

function median(values) {
  const xs = values.filter((v) => v !== null && Number.isFinite(v)).sort((a, b) => a - b);
  if (!xs.length) return null;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

function isoDateToDate(value) {
  const [y, m, d] = value.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

function dayName(value) {
  const date = isoDateToDate(value);
  return new Intl.DateTimeFormat("da-DK", { weekday: "short", timeZone: "UTC" }).format(date);
}

function csvEscape(value) {
  if (value === null || value === undefined) return "";
  const s = String(value);
  return /[;"\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
}

const rawRows = parseSemicolonCsv(await fs.readFile(sourcePath, "utf8"));
const enriched = rawRows.map((r) => {
  const cabinn = num(r["Cabinn Aarhus"]);
  const wakeup = num(r["Wakeup Aarhus"]);
  const robertas = num(r["Roberta's Society"]);
  const hostel = num(r["Aarhus Hostel og Hotel"]);
  const milling = num(r["Milling Hotel Ritz"]);
  const budgetValues = [cabinn, wakeup, hostel];
  const validBudget = budgetValues.filter((v) => v !== null).length;
  const med = validBudget >= 2 ? median(budgetValues) : null;
  const low = validBudget >= 2 ? Math.min(...budgetValues.filter((v) => v !== null)) : null;
  const high = validBudget >= 2 ? Math.max(...budgetValues.filter((v) => v !== null)) : null;
  const spread = low !== null && high !== null ? high - low : null;
  const weekday = dayName(r.dato);
  const quality = validBudget >= 3 ? "God" : validBudget === 2 ? "Brugbar" : "For lidt data";
  const note = validBudget >= 2
    ? "Median baseret på Cabinn, Wakeup og Aarhus Hostel og Hotel"
    : "Spring over: færre end to sammenlignelige budgetpriser";
  return {
    dato: r.dato,
    dateObj: isoDateToDate(r.dato),
    weekday,
    cabinn,
    wakeup,
    robertas,
    hostel,
    milling,
    validBudget,
    med,
    low,
    high,
    spread,
    quality,
    note,
  };
});

const importRows = enriched.filter((r) => r.validBudget >= 2);

const csvHeaders = [
  "dato",
  "medianpris_vaerelse",
  "medianpris_seng",
  "antal",
  "laveste_budget",
  "hoejeste_budget",
  "spredning_budget",
  "robertas_society",
  "milling_hotel_ritz",
  "datakvalitet",
  "note",
];
const csvLines = [
  csvHeaders.join(";"),
  ...importRows.map((r) => [
    r.dato,
    Math.round(r.med),
    "",
    r.validBudget,
    r.low,
    r.high,
    r.spread,
    r.robertas ?? "",
    r.milling ?? "",
    r.quality,
    r.note,
  ].map(csvEscape).join(";")),
];
await fs.writeFile(csvPath, `${csvLines.join("\n")}\n`, "utf8");

const workbook = Workbook.create();
const overview = workbook.worksheets.add("Overblik");
const importSheet = workbook.worksheets.add("Import CSV");
const sourceSheet = workbook.worksheets.add("Kilder");

for (const sheet of [overview, importSheet, sourceSheet]) {
  sheet.showGridLines = false;
}
overview.tabColor = palette.navy;
importSheet.tabColor = palette.blue;
sourceSheet.tabColor = "#AAB7C4";

sourceSheet.getRange("A1:O1").values = [[
  "Dato",
  "Dag",
  "Cabinn Aarhus",
  "Wakeup Aarhus",
  "Roberta's Society",
  "Aarhus Hostel og Hotel",
  "Milling Hotel Ritz",
  "Antal budgetpriser",
  "Median budget",
  "Laveste budget",
  "Højeste budget",
  "Spredning",
  "Datakvalitet",
  "Note",
  "Kilde",
]];
sourceSheet.getRange("A2:O37").values = enriched.map((r) => [
  r.dateObj,
  r.weekday,
  r.cabinn,
  r.wakeup,
  r.robertas,
  r.hostel,
  r.milling,
  r.validBudget,
  r.med,
  r.low,
  r.high,
  r.spread,
  r.quality,
  r.note,
  "konkurrentpriser_kilder_2026-10-03.csv",
]);

importSheet.getRange("A1:K1").values = [csvHeaders];
importSheet.getRangeByIndexes(1, 0, importRows.length, csvHeaders.length).values = importRows.map((r) => [
  r.dateObj,
  Math.round(r.med),
  null,
  r.validBudget,
  r.low,
  r.high,
  r.spread,
  r.robertas,
  r.milling,
  r.quality,
  r.note,
]);

overview.getRange("A2").values = [["Forbedret konkurrentfil"]];
overview.getRange("A3").values = [["Baseret på kildefilen fra 3. oktober 2026. Importfilen bruger medianen af de mest sammenlignelige budgetpriser og holder øvrige signaler synlige."]];
overview.getRange("A5:D5").values = [["Importdage", "Gennemsnitlig median", "Weekend median", "Dage med høj spredning"]];
const weekendMedianRefs = enriched
  .map((r, idx) => ({ r, row: idx + 9 }))
  .filter(({ r }) => (r.weekday.startsWith("lør") || r.weekday.startsWith("søn")) && r.med !== null)
  .map(({ row }) => `C${row}`)
  .join(",");
overview.getRange("A6:D6").formulas = [[
  "=COUNTA('Import CSV'!A2:A36)",
  "=AVERAGE('Import CSV'!B2:B36)",
  `=AVERAGE(${weekendMedianRefs})`,
  '=COUNTIF(Kilder!L2:L37,">150")',
]];
overview.getRange("A8:E8").values = [["Dato", "Dag", "Median budget", "Spredning", "Datakvalitet"]];
overview.getRange("A9:E44").values = enriched.map((r) => [
  r.dato,
  r.weekday,
  r.med,
  r.spread,
  r.quality,
]);

overview.getRange("G5:K5").values = [["Konkurrent", "Antal priser", "Gennemsnit", "Median", "Laveste"]];
overview.getRange("G6:K10").values = [
  ["Cabinn Aarhus", null, null, null, null],
  ["Wakeup Aarhus", null, null, null, null],
  ["Roberta's Society", null, null, null, null],
  ["Aarhus Hostel og Hotel", null, null, null, null],
  ["Milling Hotel Ritz", null, null, null, null],
];
overview.getRange("H6:K10").formulas = [
  ['=COUNT(Kilder!C2:C37)', '=AVERAGE(Kilder!C2:C37)', '=MEDIAN(Kilder!C2:C37)', '=MIN(Kilder!C2:C37)'],
  ['=COUNT(Kilder!D2:D37)', '=AVERAGE(Kilder!D2:D37)', '=MEDIAN(Kilder!D2:D37)', '=MIN(Kilder!D2:D37)'],
  ['=COUNT(Kilder!E2:E37)', '=AVERAGE(Kilder!E2:E37)', '=MEDIAN(Kilder!E2:E37)', '=MIN(Kilder!E2:E37)'],
  ['=COUNT(Kilder!F2:F37)', '=AVERAGE(Kilder!F2:F37)', '=MEDIAN(Kilder!F2:F37)', '=MIN(Kilder!F2:F37)'],
  ['=COUNT(Kilder!G2:G37)', '=AVERAGE(Kilder!G2:G37)', '=MEDIAN(Kilder!G2:G37)', '=MIN(Kilder!G2:G37)'],
];

overview.getRange("G13:J13").values = [["Brug", "Definition", "Antal dage", "Kommentar"]];
overview.getRange("G14:J17").values = [
  ["Import", "Mindst to budgetpriser", importRows.length, "Kan uploades direkte i appen"],
  ["Skip", "Færre end to budgetpriser", enriched.length - importRows.length, "Vises i kilder, men udelades fra importfilen"],
  ["Roberta's", "Hybrid-/sengesignal", enriched.filter((r) => r.robertas !== null).length, "Holdes separat fra budgetmedian"],
  ["Milling", "Øvre hotelsignal", enriched.filter((r) => r.milling !== null).length, "Holdes separat fra budgetmedian"],
];

const chart = overview.charts.add("line", [
  overview.getRange("A8:A44"),
  overview.getRange("C8:C44"),
]);
chart.setPosition("G20", "N38");
chart.title = "Median budgetpris pr. dato";
chart.titleTextStyle.typeface = font;
chart.titleTextStyle.fontSize = 12;
chart.hasLegend = false;
chart.xAxis = { axisType: "textAxis", textStyle: { typeface: font, fontSize: 10 } };
chart.yAxis = {
  numberFormatCode: "#,##0",
  numberFormatSourceLinked: false,
  textStyle: { typeface: font, fontSize: 10 },
};
chart.series.items[0].line = { fill: palette.blue, style: "solid", width: 2 };

function styleSheet(sheet, usedRange) {
  sheet.getRange(usedRange).format.font = { name: font, size: 10, color: palette.text };
  sheet.getRange(usedRange).format.verticalAlignment = "center";
}
styleSheet(sourceSheet, "A1:O37");
styleSheet(importSheet, `A1:K${importRows.length + 1}`);
styleSheet(overview, "A1:N44");

for (const sheet of [sourceSheet, importSheet]) {
  const used = sheet.getUsedRange();
  used.format.autofitColumns();
  used.format.autofitRows();
}

sourceSheet.getRange("A1:O1").format = {
  fill: palette.navy,
  font: { name: font, bold: true, color: "#FFFFFF", size: 10 },
};
importSheet.getRange("A1:K1").format = {
  fill: palette.blue,
  font: { name: font, bold: true, color: "#FFFFFF", size: 10 },
};
overview.getRange("A2").format.font = { name: font, bold: true, size: 16, color: palette.text };
overview.getRange("A3").format.font = { name: font, italic: true, size: 10, color: palette.muted };
overview.getRange("A5:D5").format = {
  fill: palette.navy,
  font: { name: font, bold: true, color: "#FFFFFF", size: 10 },
};
overview.getRange("A6:D6").format = {
  fill: palette.lightBlue,
  font: { name: font, bold: true, color: palette.text, size: 12 },
};
overview.getRange("A8:E8").format = {
  fill: palette.blue,
  font: { name: font, bold: true, color: "#FFFFFF", size: 10 },
};
overview.getRange("G5:K5").format = {
  fill: palette.blue,
  font: { name: font, bold: true, color: "#FFFFFF", size: 10 },
};
overview.getRange("G13:J13").format = {
  fill: palette.navy,
  font: { name: font, bold: true, color: "#FFFFFF", size: 10 },
};
overview.getRange("A2:N2").format.borders = { bottom: { style: "thin", color: palette.gray } };
overview.getRange("A5:D6").format.borders = { preset: "outside", style: "thin", color: "#AAB7C4" };
overview.getRange("A8:E44").format.borders = { preset: "outside", style: "thin", color: "#D0D5DD" };
overview.getRange("G5:K10").format.borders = { preset: "outside", style: "thin", color: "#D0D5DD" };
overview.getRange("G13:J17").format.borders = { preset: "outside", style: "thin", color: "#D0D5DD" };
sourceSheet.getRange("A1:O37").format.borders = { preset: "outside", style: "thin", color: "#D0D5DD" };
importSheet.getRange(`A1:K${importRows.length + 1}`).format.borders = { preset: "outside", style: "thin", color: "#D0D5DD" };

for (const sheet of [sourceSheet, importSheet, overview]) {
  sheet.getRange("A:A").setNumberFormat("yyyy-mm-dd");
}
sourceSheet.getRange("A2:A37").setNumberFormat("yyyy-mm-dd");
importSheet.getRange(`A2:A${importRows.length + 1}`).setNumberFormat("yyyy-mm-dd");
overview.getRange("B:B").format.horizontalAlignment = "center";
sourceSheet.getRange("B:B").format.horizontalAlignment = "center";
sourceSheet.getRange("C:L").format.numberFormat = "#,##0";
importSheet.getRange("B:H").format.numberFormat = "#,##0";
overview.getRange("A6:D6").format.numberFormat = "#,##0";
overview.getRange("C9:D44").format.numberFormat = "#,##0";
overview.getRange("H6:K10").format.numberFormat = "#,##0";

sourceSheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "For lidt data",
  format: { fill: "#FDECEC", font: { color: palette.red, bold: true } },
});
sourceSheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "Brugbar",
  format: { fill: "#FFF6D8", font: { color: "#805B00", bold: true } },
});
sourceSheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "God",
  format: { fill: "#E6F4EE", font: { color: palette.green, bold: true } },
});
overview.getRange("E9:E44").conditionalFormats.add("containsText", {
  text: "For lidt data",
  format: { fill: "#FDECEC", font: { color: palette.red, bold: true } },
});
overview.getRange("E9:E44").conditionalFormats.add("containsText", {
  text: "Brugbar",
  format: { fill: "#FFF6D8", font: { color: "#805B00", bold: true } },
});
overview.getRange("E9:E44").conditionalFormats.add("containsText", {
  text: "God",
  format: { fill: "#E6F4EE", font: { color: palette.green, bold: true } },
});

overview.getRange("A:A").format.columnWidth = 15;
overview.getRange("B:B").format.columnWidth = 9;
overview.getRange("B:D").format.columnWidth = 18;
overview.getRange("E:E").format.columnWidth = 14;
overview.getRange("G:G").format.columnWidth = 22;
overview.getRange("H:K").format.columnWidth = 14;
overview.getRange("J:J").format.columnWidth = 40;
sourceSheet.getRange("N:N").format.columnWidth = 58;
sourceSheet.getRange("O:O").format.columnWidth = 34;
importSheet.getRange("K:K").format.columnWidth = 58;
importSheet.getRange("A:A").format.columnWidth = 13;
importSheet.getRange("B:C").format.columnWidth = 18;
importSheet.getRange("D:D").format.columnWidth = 9;
importSheet.getRange("E:G").format.columnWidth = 15;
importSheet.getRange("H:I").format.columnWidth = 17;
importSheet.getRange("J:J").format.columnWidth = 13;
sourceSheet.getRange("A:A").format.columnWidth = 13;
sourceSheet.getRange("C:G").format.columnWidth = 18;
sourceSheet.getRange("H:L").format.columnWidth = 15;
sourceSheet.getRange("M:M").format.columnWidth = 14;

sourceSheet.freezePanes.freezeRows(1);
importSheet.freezePanes.freezeRows(1);
overview.freezePanes.freezeRows(8);

workbook.recalculate();

const overviewCheck = await workbook.inspect({
  kind: "table",
  sheetId: "Overblik",
  range: "A5:K17",
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 12,
});
console.log(overviewCheck.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "Overblik",
  autoCrop: "all",
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "konkurrentpriser_forbedret_preview.png"), new Uint8Array(await preview.arrayBuffer()));
for (const sheetName of ["Import CSV", "Kilder"]) {
  const sheetPreview = await workbook.render({
    sheetName,
    autoCrop: "all",
    scale: 1,
    format: "png",
  });
  await fs.writeFile(
    path.join(outputDir, `${sheetName.toLowerCase().replaceAll(" ", "_")}_preview.png`),
    new Uint8Array(await sheetPreview.arrayBuffer()),
  );
}

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(xlsxPath);

console.log(JSON.stringify({ xlsxPath, csvPath, rowsInSource: enriched.length, rowsInImport: importRows.length }, null, 2));
