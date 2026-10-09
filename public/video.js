/* Video Production UI — display + actions only; all rules enforced server-side. */
'use strict';
const V = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : 'idem-' + Date.now() + '-' + Math.random());

let vState = null;          // /api/video/state
let currentRun = null;      // /api/run/{id}
let reviewCtx = null;       // {runId, scriptKey, approval, estimatedCost}
let vPollTimer = null;
let settingsDirty = false;  // true while the settings form has unsaved edits — refreshes must not clobber them

const FORM_IDS = ['vsProvider', 'vsAspect', 'vsResolution', 'vsCaptions', 'vsExpressiveness', 'vsWarn', 'vsLimit'];
function captureForm() {
  const f = { fields: {}, personas: {} };
  for (const id of FORM_IDS) { const el = V(id); if (el) f.fields[id] = el.type === 'checkbox' ? el.checked : el.value; }
  for (const n of ['Ravi', 'Rik', 'Product']) {
    const a = V('vav-' + n), v = V('vvo-' + n);
    f.personas[n] = { a: a ? a.value : null, v: v ? v.value : null };
  }
  return f;
}
function restoreForm(f) {
  for (const id of FORM_IDS) {
    const el = V(id);
    if (el && id in f.fields) { if (el.type === 'checkbox') el.checked = f.fields[id]; else el.value = f.fields[id]; }
  }
  for (const n of ['Ravi', 'Rik', 'Product']) {
    const a = V('vav-' + n), v = V('vvo-' + n);
    if (a && f.personas[n].a != null) a.value = f.personas[n].a;
    if (v && f.personas[n].v != null) v.value = f.personas[n].v;
  }
}

const STATUS_LABEL = {
  awaiting_review: 'Awaiting review', approved: 'Approved', rejected: 'Rejected',
  queued_for_video: 'Queued for video', generating: 'Generating', completed: 'Video completed',
  generation_failed: 'Generation failed', cancelled: 'Cancelled', draft: 'Draft',
  queued: 'Queued', processing: 'Processing', failed: 'Failed',
};
const READINESS_LABEL = {
  not_configured: 'Not configured — upload a photo', image_uploaded: 'Ready',
  voice_not_configured: 'Ready — a random voice will be used', awaiting_provider: 'Awaiting provider avatar approval',
  fully_ready: 'Ready (photo/avatar + voice)',
};

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data.errors && data.errors.join(' ')) || data.error || ('HTTP ' + res.status));
  return data;
}

/* ------------------------------------------------------------------ */
/* State loading                                                       */
/* ------------------------------------------------------------------ */
async function loadVideoState() {
  vState = await api('/api/video/state');
  renderMockBanner();
  renderQueue();
  renderSettings();
  if (currentRun) renderScripts();          // review chips depend on approvals
  const active = vState.jobs.some((j) => j.status === 'queued' || j.status === 'processing');
  if (active && !vPollTimer) vPollTimer = setInterval(loadVideoState, 5000);
  if (!active && vPollTimer) { clearInterval(vPollTimer); vPollTimer = null; }
}

async function loadRuns() {
  const { runs } = await api('/api/runs');
  const sel = V('vRunSelect');
  sel.innerHTML = runs.length
    ? runs.map((r) => `<option value="${esc(r.id)}">${esc(r.id)}</option>`).join('')
    : '<option value="">No content runs yet — run the pipeline first</option>';
  if (runs.length) await selectRun(runs[0].id);
  else V('vScripts').innerHTML = '<div class="vempty">No content runs yet. Run today\'s pipeline in the <b>1 · Create</b> tab, then approve a script here.</div>';
}

async function selectRun(runId) {
  V('vScripts').innerHTML = '<div class="vempty">Loading run…</div>';
  currentRun = await api('/api/run/' + encodeURIComponent(runId));
  renderScripts();
}

/* ------------------------------------------------------------------ */
/* Script cards                                                        */
/* ------------------------------------------------------------------ */
function approvalFor(scriptKey) {
  if (!vState || !currentRun) return null;
  return vState.approvals.filter((a) => a.contentRunId === currentRun.id && a.scriptId === scriptKey).pop() || null;
}

function renderScripts() {
  const wrap = V('vScripts');
  const keys = Object.keys(currentRun.scripts || {});
  if (!keys.length) { wrap.innerHTML = '<div class="vempty">This run has no parsed scripts.</div>'; return; }
  wrap.innerHTML = keys.map((k) => scriptCard(k, currentRun.scripts[k])).join('');
}

function scriptCard(key, s) {
  const p = s.parsed || {};
  const approval = approvalFor(key);
  const status = (approval && approval.status) || 'awaiting_review';
  const gateOk = !s.gateProblems.length;
  const srcRows = s.sources.map((r) =>
    `<tr><td>${esc(r.fact)}</td><td>${esc(r.source)}</td><td>${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">link</a>` : '—'}</td><td>${r.verified === true ? '✅ verified' : r.verified === false ? '⚠️ unverified' : '—'}</td></tr>`).join('');
  const beats = (s.parsed && s.parsed.beats || []).map((b) =>
    `<tr><td>${esc(b.beat)}${b.speaker ? '<br><b>' + esc(b.speaker) + '</b>' : ''}</td><td>${esc(b.vo)}</td><td>${esc(b.on_screen_text)}</td><td>${esc(b.visual_direction)}</td></tr>`).join('');
  const latestJob = (vState ? vState.jobs : []).filter((j) => approval && j.approvalId === approval.id)[0];
  return `
  <div class="vcard" data-script="${key}">
    <div class="vhead">
      <div>
        <div class="vtitle">${esc(p.title_working || key)}</div>
        <div class="vmeta">Pipeline ${esc(p.pipeline || '?')} · ${s.persona === 'Dialogue' ? '🎙 podcast — hosts <b>Ravi &amp; Rik</b>' : 'voice: ' + esc(p.voice || '?') + ' → persona <b>' + esc(s.persona) + '</b>'} · ~${s.estimatedSeconds}s spoken</div>
      </div>
      <span class="chip chip-${status}">${esc(STATUS_LABEL[status] || status)}</span>
    </div>
    ${gateOk ? '' : `<div class="vgate" role="alert"><b>Validation gate — approval blocked:</b><ul>${s.gateProblems.map((g) => `<li>${esc(g)}</li>`).join('')}</ul></div>`}
    ${latestJob ? `<div class="vmeta">Latest video job: ${esc(STATUS_LABEL[latestJob.status] || latestJob.status)} (attempt ${latestJob.attemptNumber}, ${esc(latestJob.provider)})</div>` : ''}
    <details><summary>Spoken narration (${s.narrationPreview.split(/\s+/).length} words)</summary><pre>${esc(s.narrationPreview)}</pre></details>
    <details><summary>Beat structure, on-screen text & visual directions</summary>
      <div class="tscroll"><table><thead><tr><th>Beat</th><th>Voice-over</th><th>On-screen text</th><th>Visual direction</th></tr></thead><tbody>${beats}</tbody></table></div>
    </details>
    <details><summary>Sources & verification (${s.sources.length})</summary>
      <div class="tscroll"><table><thead><tr><th>Fact</th><th>Source</th><th>URL</th><th>Status</th></tr></thead><tbody>${srcRows}</tbody></table></div>
    </details>
    <div class="vactions">
      <button class="vbtn primary" ${gateOk ? '' : 'disabled'} onclick="openReview('${key}')">Approve for Video</button>
      <button class="vbtn" onclick="rejectScript('${key}')">Reject</button>
      <button class="vbtn" onclick="copyScript('${key}')">Copy Script</button>
    </div>
  </div>`;
}

async function copyScript(key) {
  await navigator.clipboard.writeText(currentRun.scripts[key].narrationPreview);
}

async function rejectScript(key) {
  const note = prompt('Optional rejection note:') ?? '';
  try {
    await api('/api/video/reject', { runId: currentRun.id, scriptKey: key, note });
    await loadVideoState();
  } catch (e) { alert(e.message); }
}

/* ------------------------------------------------------------------ */
/* Review panel + generation                                           */
/* ------------------------------------------------------------------ */
async function openReview(key) {
  try {
    const result = await api('/api/video/approve', {
      runId: currentRun.id, scriptKey: key,
      approvedBy: localStorage.getItem('approverName') || 'local user',
    });
    reviewCtx = { runId: currentRun.id, scriptKey: key, approval: result.approval, estimatedCost: result.estimatedCost };
    renderReview();
    V('vReviewDlg').showModal();
    await loadVideoState();
  } catch (e) { alert('Approval blocked:\n' + e.message); }
}

function renderReview() {
  const a = reviewCtx.approval;
  const isDialogue = a.persona === 'Dialogue';
  const persona = isDialogue ? {} : (vState.personas[a.persona] || {});
  // Always the CURRENT provider setting — generation follows it, not the approval snapshot.
  const providerInfo = vState.providerStatus[vState.settings.provider] || {};
  const asset = persona.asset;
  const est = reviewCtx.estimatedCost;
  // Generation follows CURRENT settings: photo first, then avatar ID; a
  // missing voice is picked at random from the provider and saved.
  const hostRow = (name) => {
    const p = vState.personas[name] || {};
    return `<div><span class="vlabel">Host — ${esc(name)}</span>` +
      `voice: ${esc(p.voiceId || 'random (picked automatically)')} · ref: ${p.asset ? 'photo ' + esc(p.asset.originalFilename) : p.providerAvatarId ? 'avatar ' + esc(p.providerAvatarId) : '⚠️ missing'}</div>`;
  };
  V('vReviewBody').innerHTML = `
    ${providerInfo.isMock ? '<div class="vmock">MOCK VIDEO PROVIDER — no credits will be used and no real video is produced.</div>' : ''}
    ${isDialogue ? '<div class="vmeta">🎙 Two-host podcast: each host\'s turns are generated as separate clips with that host\'s avatar and voice, delivered in order for the podcast edit.</div>' : ''}
    <div class="vgrid">
      <div><span class="vlabel">Speaker / persona</span><b>${isDialogue ? 'Ravi &amp; Rik (podcast)' : esc(a.persona)}</b>${isDialogue ? '' : ' (' + esc(a.approvedScriptSnapshot.voice || 'product') + ')'}</div>
      <div><span class="vlabel">Provider</span>${esc(providerInfo.displayName || vState.settings.provider)} — ${esc(providerInfo.detail || '')}</div>
      ${isDialogue ? hostRow('Rik') + hostRow('Ravi') : `
      <div><span class="vlabel">Voice ID</span>${esc(persona.voiceId || 'random (picked automatically)')}</div>
      <div><span class="vlabel">Reference</span>${asset ? 'Photo: ' + esc(asset.originalFilename) : persona.providerAvatarId ? 'Provider avatar: ' + esc(persona.providerAvatarId) : '⚠️ no image/avatar configured'}</div>`}
      <div><span class="vlabel">Estimated runtime</span>~${reviewCtx ? esc(String(estRuntime())) : '?'}s</div>
      <div><span class="vlabel">Estimated cost</span>${est === 0 ? '$0.00 (mock)' : est != null ? '$' + est.toFixed(2) + ' <i>(estimate)</i>' : 'unknown — provider does not publish a computable rate'}</div>
      <div><span class="vlabel">Resolution</span>${esc(a.settingsSnapshot.resolution)}</div>
      <div><span class="vlabel">Captions</span>${a.settingsSnapshot.captions ? 'Burned-in captions ON' : 'OFF'}</div>
    </div>
    ${asset ? `<img class="vrefimg" src="/api/video/asset/${esc(asset.id)}" alt="Reference image for ${esc(a.persona)}">` : ''}
    <div class="vfield"><span class="vlabel">Video format</span>
      <label><input type="radio" name="vAspect" value="16:9" checked> 16:9 landscape (podcast master)</label>
      <label><input type="radio" name="vAspect" value="9:16"> 9:16 vertical (LinkedIn / Instagram)</label>
      <label><input type="radio" name="vAspect" value="both"> Generate both ${est != null ? `(~$${(est * 2).toFixed(2)} total, estimate)` : '(two generations — double cost)'}</label>
    </div>
    <details><summary>Final spoken narration (exactly what will be voiced)</summary><pre>${esc(a.approvedScriptSnapshot.narration)}</pre></details>
    <div class="vmeta">Source validation: ${a.sourceValidationSnapshot.filter((r) => r.verified === true).length} verified, ${a.sourceValidationSnapshot.filter((r) => r.verified === false).length} unverified, ${a.sourceValidationSnapshot.length} records retained (sources are never narrated).</div>
  `;
}

function estRuntime() {
  const a = reviewCtx.approval;
  const words = a.approvedScriptSnapshot.narration.split(/\s+/).length;
  return Math.max(Math.round(words / 2.5), a.approvedScriptSnapshot.runtimeSeconds || 0);
}

async function generateFromReview() {
  const a = reviewCtx.approval;
  const aspect = document.querySelector('input[name="vAspect"]:checked').value;
  const providerInfo = vState.providerStatus[vState.settings.provider] || {};
  const est = reviewCtx.estimatedCost;
  const costLine = providerInfo.isMock ? 'This is the MOCK provider — free, and not a real video.'
    : est != null ? `Estimated cost: $${(aspect === 'both' ? est * 2 : est).toFixed(2)} (estimate).` : 'Cost estimate unavailable — this WILL consume paid provider credits.';
  if (!confirm(`Start ${aspect === 'both' ? 'TWO generations (16:9 + 9:16)' : 'a ' + aspect + ' generation'} on ${providerInfo.displayName || vState.settings.provider}?\n${costLine}`)) return;
  const btn = V('vGenerateBtn');
  btn.disabled = true; btn.textContent = 'Submitting…';
  try {
    const aspects = aspect === 'both' ? ['16:9', '9:16'] : [aspect];
    for (const ar of aspects) {
      await api('/api/video/generate', { approvalId: a.id, aspectRatio: ar, confirm: true, idempotencyKey: uuid() });
    }
    V('vReviewDlg').close();
    await loadVideoState();
    if (window.showTab) showTab('videos'); // jump to the Videos tab so the new job is visible
    V('vQueueCard').scrollIntoView({ behavior: 'smooth' });
  } catch (e) { alert('Generation blocked:\n' + e.message); }
  finally { btn.disabled = false; btn.textContent = 'Generate Video'; }
}

/* ------------------------------------------------------------------ */
/* Queue                                                               */
/* ------------------------------------------------------------------ */
function queueFilters() {
  return { status: V('vfStatus').value, persona: V('vfPersona').value,
           pipeline: V('vfPipeline').value, provider: V('vfProvider').value };
}

function renderQueue() {
  const f = queueFilters();
  const jobs = vState.jobs.filter((j) =>
    (!f.status || j.status === f.status) && (!f.persona || j.persona === f.persona) &&
    (!f.pipeline || j.pipeline === f.pipeline) && (!f.provider || j.provider === f.provider));
  V('vQueueBody').innerHTML = jobs.length ? jobs.map((j) => `
    <tr>
      <td>${esc(j.title)}<div class="vmeta">${esc(j.createdAt || '').replace('T', ' ').slice(0, 16)}</div></td>
      <td>${j.persona === 'Dialogue' ? 'Ravi &amp; Rik 🎙' : esc(j.persona)}</td><td>${esc(j.pipeline || '—')}</td>
      <td>${esc(j.provider)}${j.isMock ? ' <span class="vmocktag">MOCK</span>' : ''}</td>
      <td>#${j.attemptNumber}</td><td>${esc(j.aspectRatio)}</td>
      <td><span class="chip chip-${j.status}">${esc(STATUS_LABEL[j.status] || j.status)}</span>
          ${j.errorMessage ? `<div class="verr">${esc(j.errorMessage)}</div>` : ''}</td>
      <td>${j.actualCost != null ? '$' + j.actualCost.toFixed(2) : j.estimatedCost != null ? '~$' + j.estimatedCost.toFixed(2) : '—'}</td>
      <td class="vrowactions">${jobActions(j)}</td>
    </tr>`).join('')
    : '<tr><td colspan="9" class="vempty">No video jobs yet.</td></tr>';
}

function jobActions(j) {
  const acts = [`<button class="vbtn small" onclick="openJob('${j.id}')">Details</button>`];
  if (j.status === 'completed' && j.videoUrl) {
    acts.push(`<button class="vbtn small" onclick="previewJob('${j.id}')">Preview</button>`);
    acts.push(`<a class="vbtn small" href="${esc(j.videoUrl)}" download target="_blank" rel="noopener">Download</a>`);
    acts.push(`<button class="vbtn small" onclick="regenerate('${j.id}')">Regenerate</button>`);
  }
  if (j.status === 'failed') acts.push(`<button class="vbtn small" onclick="retryJob('${j.id}')">Retry</button>`);
  if (j.status === 'queued' || j.status === 'processing') acts.push(`<button class="vbtn small" onclick="cancelJob('${j.id}')">Cancel</button>`);
  return acts.join(' ');
}

async function cancelJob(jobId) {
  if (!confirm('Cancel this generation?')) return;
  try { await api('/api/video/cancel', { jobId }); } catch (e) { alert(e.message); }
  await loadVideoState();
}

async function retryJob(jobId) {
  const job = vState.jobs.find((j) => j.id === jobId);
  if (!confirm('Retry this failed generation? This creates a new attempt.')) return;
  try {
    await api('/api/video/generate', { approvalId: job.approvalId, aspectRatio: job.aspectRatio, confirm: true, idempotencyKey: uuid() });
  } catch (e) { alert(e.message); }
  await loadVideoState();
}

async function regenerate(jobId) {
  const job = vState.jobs.find((j) => j.id === jobId);
  if (!confirm('Regenerate another version? The existing video is kept; this creates a new attempt and may cost credits.')) return;
  try {
    await api('/api/video/generate', { approvalId: job.approvalId, aspectRatio: job.aspectRatio, confirm: true, idempotencyKey: uuid() });
  } catch (e) { alert(e.message); }
  await loadVideoState();
}

function previewJob(jobId) {
  const j = vState.jobs.find((x) => x.id === jobId);
  const isSvg = (j.videoUrl || '').endsWith('.svg');
  V('vPreviewBody').innerHTML = `
    ${j.isMock ? '<div class="vmock">MOCK VIDEO PROVIDER — placeholder output, not a real generated video.</div>' : ''}
    ${isSvg ? `<img src="${esc(j.videoUrl)}" alt="Mock placeholder" style="width:100%">`
            : `<video src="${esc(j.videoUrl)}" controls style="width:100%" preload="metadata"></video>`}
    <div class="vmeta">${esc(j.title)} · ${esc(j.aspectRatio)} · ${j.durationSeconds ? j.durationSeconds + 's' : ''}
      ${!j.isMock ? '<button class="vbtn small" onclick="refreshUrl(\'' + j.id + '\')">Refresh link</button>' : ''}</div>`;
  V('vPreviewDlg').showModal();
}

async function refreshUrl(jobId) {
  try { await api('/api/video/refresh-url', { jobId }); await loadVideoState(); previewJob(jobId); }
  catch (e) { alert(e.message); }
}

async function openJob(jobId) {
  const detail = await api('/api/video/job/' + encodeURIComponent(jobId));
  const j = detail.job, a = detail.approval || {};
  V('vPreviewBody').innerHTML = `
    ${j.isMock ? '<div class="vmock">MOCK VIDEO PROVIDER</div>' : ''}
    <h3>${esc(j.title)}</h3>
    <div class="vgrid">
      <div><span class="vlabel">Status</span><span class="chip chip-${j.status}">${esc(STATUS_LABEL[j.status] || j.status)}</span></div>
      <div><span class="vlabel">Attempt</span>#${j.attemptNumber}</div>
      <div><span class="vlabel">Provider</span>${esc(j.provider)} (${esc(j.providerJobId || 'not submitted')})</div>
      <div><span class="vlabel">Format</span>${esc(j.aspectRatio)} · ${esc(j.resolution)}</div>
      <div><span class="vlabel">Cost</span>${j.actualCost != null ? '$' + j.actualCost.toFixed(2) : j.estimatedCost != null ? '~$' + j.estimatedCost.toFixed(2) + ' (estimate)' : '—'}</div>
      <div><span class="vlabel">Approved by</span>${esc(a.approvedBy || '—')} at ${esc(a.approvedAt || '—')}</div>
    </div>
    ${j.errorMessage ? `<div class="vgate" role="alert">${esc(j.errorCode)}: ${esc(j.errorMessage)}</div>` : ''}
    ${j.segments && j.segments.length ? `<details open><summary>Podcast segments (${j.segments.length} clips, stitch in order)</summary>
      <div class="tscroll"><table><thead><tr><th>#</th><th>Host</th><th>Status</th><th>Clip</th></tr></thead><tbody>
      ${j.segments.map((s) => `<tr><td>${s.index + 1}</td><td>${esc(s.speaker)}</td><td><span class="chip chip-${s.status}">${esc(STATUS_LABEL[s.status] || s.status)}</span>${s.errorMessage ? `<div class="verr">${esc(s.errorMessage)}</div>` : ''}</td><td>${s.videoUrl ? `<a class="vbtn small" href="${esc(s.videoUrl)}" download target="_blank" rel="noopener">Download</a>` : '—'}</td></tr>`).join('')}
      </tbody></table></div></details>` : ''}
    <details><summary>Approved script snapshot</summary><pre>${esc((a.approvedScriptSnapshot || {}).narration || '')}</pre></details>
    <details><summary>Status timeline (${detail.events.length})</summary>
      <div class="tscroll"><table><thead><tr><th>Time</th><th>Event</th><th>Provider status</th></tr></thead><tbody>
        ${detail.events.map((e) => `<tr><td>${esc(e.createdAt.replace('T', ' ').slice(0, 19))}</td><td>${esc(e.eventType)}</td><td>${esc(e.providerStatus)} ${esc(JSON.stringify(e.normalizedDetails))}</td></tr>`).join('')}
      </tbody></table></div></details>`;
  V('vPreviewDlg').showModal();
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */
function renderMockBanner() {
  V('vMockBanner').classList.toggle('hidden', !vState.activeProviderIsMock);
}

function renderSettings() {
  const s = vState.settings;
  if (!settingsDirty) {
    V('vsProvider').value = s.provider;
    V('vsAspect').value = s.defaultAspectRatio;
    V('vsResolution').value = s.defaultResolution;
    V('vsCaptions').checked = !!s.captionsEnabled;
    V('vsExpressiveness').value = s.expressiveness || 'medium';
    V('vsWarn').value = s.costWarningThreshold ?? '';
    V('vsLimit').value = s.monthlyHardLimit ?? '';
  }
  V('vsSpend').textContent = `Real-provider spend this month: $${vState.monthSpend.toFixed(2)}` +
    (s.monthlyHardLimit ? ` of $${Number(s.monthlyHardLimit).toFixed(2)} limit` : ' (no hard limit set)');
  V('vsProviderStatus').innerHTML = Object.entries(vState.providerStatus).map(([name, p]) =>
    `<div>${esc(p.displayName)}${p.isMock ? ' <span class="vmocktag">MOCK</span>' : ''} — <b>${esc(p.status)}</b>: ${esc(p.detail)}</div>`).join('');
  for (const prov of ['heygen', 'tavus']) {
    const p = vState.providerStatus[prov] || {};
    V('vkeystatus-' + prov).textContent = p.keyConfigured
      ? `Configured ${p.keyHint} — ${p.detail || 'ready'}`
      : 'Not configured — paste a key above to enable real generations.';
  }
  V('vsWebhook').textContent = vState.webhookConfigured
    ? 'Webhook: public URL configured (signed events verified server-side).'
    : 'Webhook: not configured — using server-side polling every 15s (fine for local use).';
  if (settingsDirty) return; // never rebuild the persona inputs over unsaved edits
  V('vsPersonas').innerHTML = Object.entries(vState.personas).map(([name, p]) => `
    <div class="vpersona">
      <div class="vhead"><b>${esc(name)}</b><span class="chip chip-${p.readiness === 'fully_ready' ? 'completed' : p.readiness === 'not_configured' ? 'failed' : 'processing'}">${esc(READINESS_LABEL[p.readiness] || p.readiness)}</span></div>
      ${p.asset ? `<img class="vrefimg small" src="/api/video/asset/${esc(p.asset.id)}" alt="Reference image for ${esc(name)}">` : '<div class="vmeta">No reference image uploaded.</div>'}
      <label class="vlabel" for="vup-${name}">Upload reference image (PNG/JPEG, ≥256px, ≤8MB)</label>
      <input type="file" id="vup-${name}" accept="image/png,image/jpeg" onchange="uploadAsset('${name}', this)">
      <label class="vlabel" for="vav-${name}">Provider avatar / replica ID (optional — ignored when a photo is uploaded)</label>
      <input type="text" id="vav-${name}" value="${esc(p.providerAvatarId || '')}" placeholder="e.g. HeyGen avatar_id or Tavus replica_id">
      <label class="vlabel" for="vvo-${name}">Voice ID (leave blank = random voice)</label>
      <input type="text" id="vvo-${name}" value="${esc(p.voiceId || '')}" placeholder="blank = random voice, picked once and saved">
      <button class="vbtn small" onclick="browseVoices('${name}')">Browse voices…</button>
    </div>`).join('');
}

async function uploadAsset(persona, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const b64 = await new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(',')[1]);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
  try {
    await api('/api/video/asset', { persona, filename: file.name, dataBase64: b64 });
    // Refresh to show the new image, but keep any other unsaved form edits.
    const snapshot = settingsDirty ? captureForm() : null;
    settingsDirty = false;
    await loadVideoState();
    if (snapshot) { restoreForm(snapshot); settingsDirty = true; }
  } catch (e) { alert('Upload rejected: ' + e.message); }
}

async function saveSettings() {
  // A pasted-but-unsaved provider key counts as part of "Save settings".
  for (const prov of ['heygen', 'tavus']) {
    const k = V('vkey-' + prov).value.trim();
    if (k) await saveProviderKey(prov, k);
  }
  const personas = {};
  for (const name of ['Ravi', 'Rik', 'Product']) {
    personas[name] = { providerAvatarId: V('vav-' + name).value.trim() || null,
                       voiceId: V('vvo-' + name).value.trim() || null };
  }
  try {
    await api('/api/video/settings', {
      provider: V('vsProvider').value,
      defaultAspectRatio: V('vsAspect').value,
      defaultResolution: V('vsResolution').value,
      captionsEnabled: V('vsCaptions').checked,
      expressiveness: V('vsExpressiveness').value,
      costWarningThreshold: V('vsWarn').value ? Number(V('vsWarn').value) : null,
      monthlyHardLimit: V('vsLimit').value ? Number(V('vsLimit').value) : null,
      personas,
    });
    settingsDirty = false;
    await loadVideoState();
    V('vsSaved').textContent = 'Saved ✓';
    setTimeout(() => (V('vsSaved').textContent = ''), 1500);
  } catch (e) { alert(e.message); }
}

/* ------------------------------------------------------------------ */
async function browseVoices(persona) {
  V('vPreviewBody').innerHTML = '<div class="vempty">Loading voices from HeyGen…</div>';
  V('vPreviewDlg').showModal();
  try {
    const { voices } = await api('/api/video/provider-voices?provider=heygen');
    if (!voices.length) { V('vPreviewBody').innerHTML = '<div class="vempty">No voices returned by HeyGen.</div>'; return; }
    V('vPreviewBody').innerHTML = `
      <h3>Pick a voice for ${esc(persona)}</h3>
      <div class="vmeta">Your cloned/private voices are listed first. Click Use, then Save settings.</div>
      <div class="tscroll" style="max-height:420px;overflow-y:auto"><table>
        <thead><tr><th>Name</th><th>Language</th><th>Gender</th><th>Type</th><th>Preview</th><th></th></tr></thead>
        <tbody>${voices.map((v) => `
          <tr>
            <td>${esc(v.name)}</td><td>${esc(v.language)}</td><td>${esc(v.gender)}</td>
            <td>${v.type === 'private' ? '<b>your voice</b>' : esc(v.type)}</td>
            <td>${v.previewUrl ? `<audio controls preload="none" src="${esc(v.previewUrl)}" style="height:26px;max-width:180px"></audio>` : '—'}</td>
            <td><button class="vbtn small" onclick="pickVoice('${esc(persona)}','${esc(v.voiceId)}')">Use</button></td>
          </tr>`).join('')}
        </tbody></table></div>`;
  } catch (e) {
    V('vPreviewBody').innerHTML = `<div class="vgate">Could not load voices: ${esc(e.message)}</div>`;
  }
}

function pickVoice(persona, voiceId) {
  const input = V('vvo-' + persona);
  if (input) { input.value = voiceId; settingsDirty = true; }
  V('vPreviewDlg').close();
  V('vsSaved').textContent = `Voice selected for ${persona} — click Save settings to keep it.`;
}

async function saveProviderKey(prov, key) {
  const status = V('vkeystatus-' + prov);
  status.textContent = key ? 'Verifying with the provider…' : 'Clearing…';
  try {
    const r = await api('/api/video/provider-key', { provider: prov, apiKey: key });
    V('vkey-' + prov).value = '';
    status.textContent = r.warning ? '⚠️ ' + r.warning : (r.configured ? 'Saved ✓' : 'Cleared ✓');
    // Refresh status but keep any other unsaved form edits intact.
    const snapshot = settingsDirty ? captureForm() : null;
    settingsDirty = false;
    await loadVideoState();
    if (snapshot) { restoreForm(snapshot); settingsDirty = true; }
  } catch (e) { status.textContent = '❌ ' + e.message; }
}

window.addEventListener('DOMContentLoaded', async () => {
  V('vRunSelect').addEventListener('change', (e) => selectRun(e.target.value));
  V('vGenerateBtn').addEventListener('click', generateFromReview);
  V('vsSave').addEventListener('click', saveSettings);
  // Any edit inside the settings card marks the form dirty so background
  // refreshes stop overwriting it until the user saves.
  const settingsCard = document.getElementById('vSettingsCard');
  settingsCard.addEventListener('input', () => { settingsDirty = true; });
  settingsCard.addEventListener('change', () => { settingsDirty = true; });
  for (const prov of ['heygen', 'tavus']) {
    V('vkeysave-' + prov).addEventListener('click', () => {
      const key = V('vkey-' + prov).value.trim();
      if (!key) { V('vkeystatus-' + prov).textContent = 'Paste a key first.'; return; }
      saveProviderKey(prov, key);
    });
    V('vkeyclear-' + prov).addEventListener('click', () => {
      if (confirm('Remove the saved ' + prov + ' API key from this computer?')) saveProviderKey(prov, '');
    });
  }
  for (const id of ['vfStatus', 'vfPersona', 'vfPipeline', 'vfProvider'])
    V(id).addEventListener('change', renderQueue);
  try { await loadVideoState(); await loadRuns(); }
  catch (e) { V('vScripts').innerHTML = `<div class="vgate">Video area failed to load: ${esc(e.message)}</div>`; }
});
