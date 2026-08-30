/**
 * Penerima webhook untuk Crypto-MEX.
 *
 * Menerima {kind: "event"|"trade"|"run", row: {...}} dari GitHub Actions dan
 * menambahkannya ke sheet yang sesuai. Header dibuat otomatis dari baris
 * pertama, dan kolom baru ditambahkan di kanan tanpa merusak data lama.
 */
var SHEETS = { event: 'events', trade: 'trades', run: 'runs' };

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    var name = SHEETS[body.kind];
    if (!name) return _json({ ok: false, error: 'kind tidak dikenal: ' + body.kind });

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sh = ss.getSheetByName(name) || ss.insertSheet(name);
    var row = body.row || {};

    var header = sh.getLastRow() > 0
      ? sh.getRange(1, 1, 1, Math.max(sh.getLastColumn(), 1)).getValues()[0]
      : [];
    if (header.length === 0 || (header.length === 1 && header[0] === '')) {
      header = Object.keys(row);
      sh.getRange(1, 1, 1, header.length).setValues([header]);
      sh.setFrozenRows(1);
      sh.getRange(1, 1, 1, header.length).setFontWeight('bold');
    } else {
      // Kolom baru ditambahkan di kanan, data lama tidak digeser.
      var missing = Object.keys(row).filter(function (k) { return header.indexOf(k) < 0; });
      if (missing.length) {
        sh.getRange(1, header.length + 1, 1, missing.length).setValues([missing]);
        header = header.concat(missing);
      }
    }

    var line = header.map(function (k) {
      var v = row[k];
      return (v === undefined || v === null) ? '' : v;
    });
    sh.appendRow(line);
    return _json({ ok: true, sheet: name, row: sh.getLastRow() });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

function doGet() {
  return _json({ ok: true, service: 'crypto-mex logger' });
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
