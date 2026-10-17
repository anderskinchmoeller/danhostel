/* Planning only. The server validates before drawing arms. */
(function () {
  'use strict';
  const form = document.querySelector('form[action="/resultater/ab/opret"]');
  if (!form) return;
  function update() {
    const sigma = Number(form.elements.sigma.value);
    const delta = Number(form.elements.delta.value);
    const pairs = Number(form.elements.pairs.value);
    const days = Number(form.elements.block_days.value);
    const output = document.getElementById('ab-power');
    if (!(sigma > 0 && delta > 0 && Number.isFinite(sigma / delta))) {
      output.textContent = 'Angiv en positiv parspredning og mindste relevante forbedring.';
      return;
    }
    const required = Math.max(6, Math.ceil(Math.pow(2.801585218112969 * sigma / delta, 2)));
    output.textContent = 'Planlægningsskøn: mindst ' + required + ' par. Dit valg: ' + pairs +
      ' par, ' + (2 * pairs * days / 7) + ' uger med ophold, plus prisperioden før første ophold og afstemning.' +
      (pairs < required ? ' Antallet er for lavt til de valgte forudsætninger.' : '') +
      ' Dette er et skøn, ikke en garanti for testens styrke.';
  }
  form.addEventListener('input', update);
  update();
}());
