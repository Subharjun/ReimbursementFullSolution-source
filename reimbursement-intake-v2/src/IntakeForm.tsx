import { useState, useRef, useEffect } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import './IntakeForm.css';

interface ProgressStep {
  key: string;
  label: string;
  status: 'done' | 'active' | 'pending' | 'faulted';
  link?: string | null;
}
interface CaseProgress {
  job_key: string;
  instance_status: string | null;
  steps: ProgressStep[];
  review_link?: string | null;
  done: boolean;
}

// Shown before the first real poll returns (and if progress is unavailable).
const DEFAULT_STEPS: ProgressStep[] = [
  { key: 'submitted', label: 'Submitted', status: 'done' },
  { key: 'intake', label: 'Intake & IDP', status: 'active' },
  { key: 'classify', label: 'Classification & Policy', status: 'pending' },
  { key: 'review', label: 'Human Review', status: 'pending' },
  { key: 'payout', label: 'Payment & Closure', status: 'pending' },
];

const STEP_CLASS: Record<ProgressStep['status'], string> = {
  done: 'pipeline-step--done',
  active: 'pipeline-step--active',
  pending: '',
  faulted: 'pipeline-step--faulted',
};

// The VALUE must be a policy category key, verbatim.
//
// PolicyRuleCheckWorkflow looks the category up case-sensitively with no
// normalisation — `pol[expense_type] || pol.others` — so any label that is not
// an exact key silently falls back to `others` (spend limit 5,000,
// auto_approve_threshold 0) instead of erroring. The old free-text labels
// ('Meals & Entertainment', 'Accommodation', …) missed on every single one,
// including 'Travel' and 'Medical', purely on capitalisation. The Case only
// worked because ReimbursementClassificationAgent re-derived the category from
// the intake email and emitted a correct lowercase key.
//
// consensus/agents.py already reasons in these same keys, so this alignment
// fixes the local engine's branches too. Keep in sync with the policy DB in
// caseplan.json (Stage_cdgcAk/tSJN1JDM2 `policy_json`) and consensus/vision.py.
const EXPENSE_TYPES: { value: string; label: string }[] = [
  { value: 'travel', label: 'Travel & Accommodation' },
  { value: 'food', label: 'Meals & Food' },
  { value: 'medical', label: 'Medical' },
  { value: 'internet', label: 'Internet & Utilities' },
  { value: 'equipment', label: 'Equipment & Supplies' },
  { value: 'others', label: 'Other' },
];

const CURRENCIES = ['INR', 'USD', 'EUR', 'GBP', 'AED'];

interface Fields {
  employeeName: string;
  employeeEmail: string;
  managerEmail: string;
  expenseType: string;
  vendor: string;
  amount: string;
  currency: string;
  date: string;
  purpose: string;
}

type SubmitState =
  | { status: 'idle' }
  | { status: 'submitting' }
  | { status: 'success'; caseId: string; jobId?: string }
  | { status: 'error'; message: string };

interface ExtractResult {
  ok: boolean;
  readable?: boolean;
  confidence?: number;
  summary?: string;
  note?: string;
  fields?: {
    vendor: string;
    amount: number | null;
    currency: string;
    date: string;
    expenseType: string;
  };
}

type ExtractState =
  | { status: 'idle' }
  | { status: 'reading' }
  | { status: 'done'; confidence?: number; summary?: string; note?: string };

interface IntakeFormProps {
  darkTheme: boolean;
  onToggleTheme: () => void;
}

// ── Ask the Committee — a copilot grounded in THIS claim's real record ──────
interface ChatMsg { who: 'you' | 'clerk'; text: string }

const COPILOT_SUGGESTIONS = [
  'Why is my claim at this stage?',
  'What did the AI committee decide?',
  'Did any agent disagree?',
  'What happens next?',
];

function CommitteeCopilot({ claimRef }: { claimRef: string }) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: 'smooth' });
  }, [msgs]);

  const ask = async (q: string) => {
    const question = q.trim();
    if (!question || busy) return;
    setMsgs((m) => [...m, { who: 'you', text: question }]);
    setDraft('');
    setBusy(true);
    try {
      const r = await fetch(`/api/copilot/${encodeURIComponent(claimRef)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      const d = await r.json();
      setMsgs((m) => [...m, { who: 'clerk', text: d.answer || 'No answer available right now.' }]);
    } catch {
      setMsgs((m) => [...m, { who: 'clerk', text: 'I lost the connection — try again in a moment.' }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="copilot">
      <div className="copilot-head">
        <span className="copilot-title">🏛️ Ask the Committee</span>
        <span className="copilot-sub">answers grounded in your claim's real record — the agents' debate, policy limits, live case status</span>
      </div>
      {msgs.length === 0 && (
        <div className="copilot-suggest">
          {COPILOT_SUGGESTIONS.map((s) => (
            <button key={s} type="button" onClick={() => ask(s)} disabled={busy}>{s}</button>
          ))}
        </div>
      )}
      {msgs.length > 0 && (
        <div className="copilot-feed" ref={feedRef}>
          {msgs.map((m, i) => (
            <div key={i} className={`copilot-msg copilot-msg--${m.who}`}>
              {m.who === 'clerk' && <span className="copilot-av" aria-hidden="true">🏛️</span>}
              <span className="copilot-bubble">{m.text}</span>
            </div>
          ))}
          {busy && (
            <div className="copilot-msg copilot-msg--clerk">
              <span className="copilot-av" aria-hidden="true">🏛️</span>
              <span className="copilot-bubble copilot-bubble--typing">the clerk is consulting the record…</span>
            </div>
          )}
        </div>
      )}
      <div className="copilot-input">
        <input
          type="text"
          value={draft}
          maxLength={600}
          placeholder="e.g. why does this need a human review?"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') ask(draft); }}
          disabled={busy}
        />
        <button type="button" onClick={() => ask(draft)} disabled={busy || !draft.trim()}>Ask</button>
      </div>
    </div>
  );
}

const defaultFields: Fields = {
  employeeName: '',
  employeeEmail: '',
  managerEmail: '',
  expenseType: '',
  vendor: '',
  amount: '',
  currency: 'INR',
  date: '',
  purpose: '',
};

function IntakeForm({ darkTheme, onToggleTheme }: IntakeFormProps) {
  const [fields, setFields] = useState<Fields>(defaultFields);
  const [receipt, setReceipt] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [submitState, setSubmitState] = useState<SubmitState>({ status: 'idle' });
  const [triedSubmit, setTriedSubmit] = useState(false);
  const [progress, setProgress] = useState<CaseProgress | null>(null);
  const [extract, setExtract] = useState<ExtractState>({ status: 'idle' });
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Once a claim is submitted, poll the REAL MirCaseClone stage cursor from
  // Orchestrator (element-executions) so the pipeline below is live, not a
  // hardcoded animation. Stops when the case reaches a terminal state.
  const jobId = submitState.status === 'success' ? submitState.jobId : undefined;
  useEffect(() => {
    if (!jobId) { setProgress(null); return; }
    let active = true;
    const poll = async () => {
      try {
        const r = await fetch(`/api/case/${jobId}/progress`, { cache: 'no-store' });
        if (!r.ok) return;
        const d: CaseProgress = await r.json();
        if (active) setProgress(d);
        if (d.done) clearInterval(timer);
      } catch { /* transient — next tick retries */ }
    };
    poll();
    const timer = setInterval(poll, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [jobId]);

  const handleChange = (
    e: ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>,
  ) => {
    const { name, value } = e.target;
    setFields((prev) => ({ ...prev, [name]: value }));
  };

  const applyFile = (file: File | undefined) => {
    if (!file) return;
    const allowed = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'application/pdf'];
    if (!allowed.includes(file.type) && !file.name.match(/\.(jpg|jpeg|png|webp|heic|pdf)$/i)) {
      alert('Please upload an image (JPG, PNG, WEBP, HEIC) or PDF.');
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      alert('File too large. Max 10 MB.');
      return;
    }
    setReceipt(file);
    autoExtract(file);
  };

  // Groq vision auto-fill: read the dropped receipt and pre-fill blank fields.
  // Advisory only — never overwrites what the user already typed, and a failure
  // silently leaves the form manual.
  const autoExtract = async (file: File) => {
    const isImage = file.type.startsWith('image/') || /\.(jpg|jpeg|png|webp|heic)$/i.test(file.name);
    if (!isImage) return;
    setExtract({ status: 'reading' });
    try {
      const fd = new FormData();
      fd.append('receipt', file);
      const r = await fetch('/api/extract-receipt', { method: 'POST', body: fd });
      const d: ExtractResult = await r.json();
      if (!d.ok || !d.fields) {
        setExtract({ status: 'idle' });
        return;
      }
      const f = d.fields;
      setFields((prev) => ({
        ...prev,
        vendor: prev.vendor.trim() || f.vendor || '',
        amount: prev.amount.trim() || (f.amount != null ? String(f.amount) : ''),
        currency: f.currency && CURRENCIES.includes(f.currency) ? f.currency : prev.currency,
        date: prev.date.trim() || f.date || '',
        expenseType: prev.expenseType || f.expenseType || '',
        purpose: prev.purpose.trim() || d.summary || '',
      }));
      setExtract({ status: 'done', confidence: d.confidence, summary: d.summary, note: d.note });
    } catch {
      setExtract({ status: 'idle' });
    }
  };

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    applyFile(e.target.files?.[0]);
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    applyFile(e.dataTransfer.files?.[0]);
  };

  const fieldErrors = {
    employeeName: !fields.employeeName.trim() ? 'Full name is required.' : '',
    employeeEmail: !fields.employeeEmail.trim() ? 'Employee email is required.' : '',
    expenseType: !fields.expenseType ? 'Please select an expense type.' : '',
    vendor: !fields.vendor.trim() ? 'Vendor / merchant is required.' : '',
    amount: !fields.amount.trim() || Number(fields.amount) <= 0 ? 'Enter a valid amount greater than 0.' : '',
    date: !fields.date.trim() ? 'Date of expense is required.' : '',
    purpose: !fields.purpose.trim() ? 'Business purpose is required.' : '',
  };

  const isValid = Object.values(fieldErrors).every((e) => !e);

  const err = (field: keyof typeof fieldErrors) =>
    triedSubmit && fieldErrors[field] ? (
      <span className="field-error">{fieldErrors[field]}</span>
    ) : null;

  const hasErr = (field: keyof typeof fieldErrors) =>
    triedSubmit && !!fieldErrors[field];

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!isValid) {
      setTriedSubmit(true);
      return;
    }
    setSubmitState({ status: 'submitting' });

    const body = new FormData();
    Object.entries(fields).forEach(([k, v]) => body.append(k, v));
    if (receipt) body.append('receipt', receipt, receipt.name);

    try {
      const res = await fetch('/api/submit', { method: 'POST', body });
      const data = await res.json();
      if (!res.ok) {
        setSubmitState({ status: 'error', message: data.detail ?? 'Submission failed.' });
        return;
      }
      setSubmitState({ status: 'success', caseId: data.case_id, jobId: data.job_id });
    } catch (err) {
      setSubmitState({
        status: 'error',
        message: err instanceof Error ? err.message : 'Network error — is the API running?',
      });
    }
  };

  const handleReset = () => {
    setFields(defaultFields);
    setReceipt(null);
    setExtract({ status: 'idle' });
    setSubmitState({ status: 'idle' });
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  if (submitState.status === 'success') {
    return (
      <div className="intake-app">
        <header className="intake-header">
          <div className="intake-header__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            </svg>
          </div>
          <div className="intake-header__titles">
            <h1 className="intake-header__title">Reimbursement Request</h1>
            <p className="intake-header__subtitle">UiPath Maestro — Automated Processing Pipeline</p>
          </div>
          <div className="intake-header__actions">
            <button type="button" className="theme-toggle" onClick={onToggleTheme} aria-label="Toggle theme">
              {darkTheme ? (
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
                </svg>
              ) : (
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                </svg>
              )}
            </button>
          </div>
        </header>

        <div className="success-card form-container--enter">
          <div className="success-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <path d="M22 4 12 14.01l-3-3" />
            </svg>
          </div>
          <h2 className="success-title">Request Submitted</h2>
          <p className="success-subtitle">
            Your reimbursement is now being processed by the UiPath Maestro pipeline. You&apos;ll receive
            an email notification once a decision is made.
          </p>

          <div className="success-details">
            <div className="success-row">
              <span className="success-label">Case ID</span>
              <span className="success-value success-value--mono">{submitState.caseId}</span>
            </div>
            {submitState.jobId && (
              <div className="success-row">
                <span className="success-label">Job ID</span>
                <span className="success-value success-value--mono">{submitState.jobId}</span>
              </div>
            )}
            <div className="success-row">
              <span className="success-label">Employee</span>
              <span className="success-value">{fields.employeeName} ({fields.employeeEmail})</span>
            </div>
            <div className="success-row">
              <span className="success-label">Amount</span>
              <span className="success-value">{fields.currency} {fields.amount}</span>
            </div>
            <div className="success-row">
              <span className="success-label">Vendor</span>
              <span className="success-value">{fields.vendor}</span>
            </div>
          </div>

          <div className="success-pipeline">
            {(progress?.steps ?? DEFAULT_STEPS).map((s, i, arr) => (
              <span key={s.key} style={{ display: 'contents' }}>
                <span className={`pipeline-step ${STEP_CLASS[s.status]}`}>
                  {s.status === 'active' && <span className="pipeline-pulse" aria-hidden="true" />}
                  {s.label}
                </span>
                {i < arr.length - 1 && <span className="pipeline-arrow" aria-hidden="true">→</span>}
              </span>
            ))}
          </div>
          <p className="pipeline-live-hint">
            {progress?.done
              ? (progress.instance_status === 'Completed'
                  ? '✓ Case completed — payout & notifications dispatched.'
                  : `Case ${String(progress.instance_status).toLowerCase()}.`)
              : progress
                ? '● Live — tracking the real MirCaseClone case as it moves through Orchestrator.'
                : '● Connecting to the live case…'}
          </p>
          {progress?.review_link && !progress.done && (
            <a className="pipeline-review-link" href={progress.review_link} target="_blank" rel="noopener noreferrer">
              A reviewer is deciding this claim → open the Action Center task
            </a>
          )}

          {jobId && <CommitteeCopilot claimRef={jobId} />}

          <button type="button" className="outcome-btn outcome-btn--secondary" onClick={handleReset}>
            Submit another request
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="intake-app">
      <header className="intake-header">
        <div className="intake-header__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
        </div>
        <div className="intake-header__titles">
          <h1 className="intake-header__title">Reimbursement Request</h1>
          <p className="intake-header__subtitle">UiPath Maestro — Automated Processing Pipeline</p>
        </div>
        <div className="intake-header__actions">
          <button type="button" className="theme-toggle" onClick={onToggleTheme} aria-label="Toggle theme">
            {darkTheme ? (
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
              </svg>
            )}
          </button>
        </div>
      </header>

      <form className="form-container form-container--enter" onSubmit={handleSubmit} noValidate>

        {/* ── Employee Info ───────────────────────────── */}
        <section className="form-section">
          <h2 className="form-title">Employee Information</h2>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="employeeName">Full Name <span className="req">*</span></label>
              <input
                id="employeeName"
                name="employeeName"
                type="text"
                placeholder="Subharjun Bose"
                value={fields.employeeName}
                onChange={handleChange}
                required
                autoComplete="name"
                className={hasErr('employeeName') ? 'input-error' : ''}
              />
              {err('employeeName')}
            </div>
            <div className="form-group">
              <label htmlFor="employeeEmail">Employee Email <span className="req">*</span></label>
              <input
                id="employeeEmail"
                name="employeeEmail"
                type="email"
                placeholder="you@company.com"
                value={fields.employeeEmail}
                onChange={handleChange}
                required
                autoComplete="email"
                className={hasErr('employeeEmail') ? 'input-error' : ''}
              />
              {err('employeeEmail')}
            </div>
            <div className="form-group">
              <label htmlFor="managerEmail">Manager Email</label>
              <input
                id="managerEmail"
                name="managerEmail"
                type="email"
                placeholder="manager@company.com"
                value={fields.managerEmail}
                onChange={handleChange}
                autoComplete="email"
              />
            </div>
          </div>
        </section>

        {/* ── Expense Details ─────────────────────────── */}
        <section className="form-section">
          <h2 className="form-title">Expense Details</h2>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="expenseType">Expense Type <span className="req">*</span></label>
              <select
                id="expenseType"
                name="expenseType"
                value={fields.expenseType}
                onChange={handleChange}
                required
                className={hasErr('expenseType') ? 'input-error' : ''}
              >
                <option value="" disabled>Select a category…</option>
                {EXPENSE_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
              {err('expenseType')}
            </div>
            <div className="form-group">
              <label htmlFor="vendor">Vendor / Merchant <span className="req">*</span></label>
              <input
                id="vendor"
                name="vendor"
                type="text"
                placeholder="e.g. Swiggy, IndiGo, Marriott"
                value={fields.vendor}
                onChange={handleChange}
                required
                className={hasErr('vendor') ? 'input-error' : ''}
              />
              {err('vendor')}
            </div>
            <div className="form-group form-group--amount">
              <label htmlFor="amount">Amount <span className="req">*</span></label>
              <div className="amount-row">
                <select
                  id="currency"
                  name="currency"
                  value={fields.currency}
                  onChange={handleChange}
                  className="currency-select"
                >
                  {CURRENCIES.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
                <input
                  id="amount"
                  name="amount"
                  type="text"
                  inputMode="decimal"
                  placeholder="0.00"
                  value={fields.amount}
                  onChange={handleChange}
                  required
                  className={`amount-input${hasErr('amount') ? ' input-error' : ''}`}
                />
              </div>
              {err('amount')}
            </div>
            <div className="form-group">
              <label htmlFor="date">Date of Expense <span className="req">*</span></label>
              <input
                id="date"
                name="date"
                type="date"
                value={fields.date}
                onChange={handleChange}
                max={new Date().toISOString().split('T')[0]}
                required
                className={hasErr('date') ? 'input-error' : ''}
              />
              {err('date')}
            </div>
          </div>
          <div className="form-group form-group--full">
            <label htmlFor="purpose">Business Purpose <span className="req">*</span></label>
            <textarea
              id="purpose"
              name="purpose"
              rows={3}
              placeholder="Briefly describe why this expense was incurred and how it relates to business activity…"
              value={fields.purpose}
              onChange={handleChange}
              required
              className={hasErr('purpose') ? 'input-error' : ''}
            />
            {err('purpose')}
          </div>
        </section>

        {/* ── Receipt Upload ──────────────────────────── */}
        <section className="form-section">
          <h2 className="form-title">Receipt</h2>
          <div
            className={`drop-zone ${dragOver ? 'drop-zone--over' : ''} ${receipt ? 'drop-zone--filled' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && fileInputRef.current?.click()}
            aria-label="Receipt upload area"
          >
            {receipt ? (
              <>
                <div className="drop-zone__icon drop-zone__icon--success" aria-hidden="true">
                  <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <path d="M14 2v6h6M9 15l2 2 4-4" />
                  </svg>
                </div>
                <p className="drop-zone__name">{receipt.name}</p>
                <p className="drop-zone__size">{(receipt.size / 1024).toFixed(0)} KB — click to replace</p>
              </>
            ) : (
              <>
                <div className="drop-zone__icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="17 8 12 3 7 8" />
                    <line x1="12" y1="3" x2="12" y2="15" />
                  </svg>
                </div>
                <p className="drop-zone__label">Drop receipt here or <span className="drop-zone__link">browse</span></p>
                <p className="drop-zone__hint">JPG, PNG, WEBP, HEIC, PDF — max 10 MB</p>
              </>
            )}
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*,.pdf"
              onChange={handleFileChange}
              className="drop-zone__input"
              tabIndex={-1}
            />
          </div>

          {/* ── Groq vision auto-fill status ─────────────── */}
          {extract.status === 'reading' && (
            <div className="extract-banner extract-banner--reading">
              <span className="extract-spinner" aria-hidden="true" />
              Reading your receipt with AI vision — fields will fill in automatically…
            </div>
          )}
          {extract.status === 'done' && (
            <div className="extract-banner extract-banner--done">
              <span aria-hidden="true">✨</span>
              <div>
                <strong>{extract.note || 'Auto-filled from your receipt — please confirm before submitting.'}</strong>
                {typeof extract.confidence === 'number' && (
                  <span className="extract-conf"> · read confidence {Math.round(extract.confidence * 100)}%</span>
                )}
              </div>
            </div>
          )}
        </section>

        {/* ── Error banner ────────────────────────────── */}
        {submitState.status === 'error' && (
          <div className="error-banner" role="alert">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            {submitState.message}
          </div>
        )}

        {/* ── Submit ──────────────────────────────────── */}
        <div className="form-buttons">
          <p className="form-hint">
            Fields marked <span className="req">*</span> are required.
            Your request will be routed through AI classification, policy check, and human review before payout.
          </p>
          <button
            type="submit"
            className="outcome-btn outcome-btn--primary"
            disabled={submitState.status === 'submitting'}
          >
            {submitState.status === 'submitting' ? (
              <>
                <span className="spinner" aria-hidden="true" />
                Submitting…
              </>
            ) : (
              'Submit Request'
            )}
          </button>
        </div>
      </form>
    </div>
  );
}

export default IntakeForm;
