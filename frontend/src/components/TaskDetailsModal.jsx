import React, { useEffect, useState } from 'react';

export default function TaskDetailsModal({ task, onClose }) {
  const [history, setHistory] = useState([]);
  const [loadingHistory, setLoadingHistory] = useState(false);

  useEffect(() => {
    if (!task || !task.task_id) return;

    let mounted = true;
    setLoadingHistory(true);

    fetch(`/api/tasks/${encodeURIComponent(task.task_id)}/history`)
      .then((res) => res.json())
      .then((data) => {
        if (mounted && data.status === 'SUCCESS') {
          setHistory(data.history || []);
        }
      })
      .catch((err) => {
        console.error('Error fetching task history:', err);
      })
      .finally(() => {
        if (mounted) setLoadingHistory(false);
      });

    return () => {
      mounted = false;
    };
  }, [task]);

  if (!task) return null;

  const taskName = task.task_name || task.task || 'Untitled Task';
  const jiraKey = task.jira_issue_key || task.jira_key;
  const assignee = task.assignee || 'Unassigned';
  const deadline = task.deadline || 'None specified';
  const currStatus = task.current_status || task.status || 'PENDING';
  const prevStatus = task.previous_status || 'None (Initial introduction)';
  const evidence = task.completion_evidence || task.reason || 'None reported yet';

  const formatDate = (isoStr) => {
    if (!isoStr) return 'N/A';
    try {
      const d = new Date(isoStr);
      return d.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch {
      return isoStr;
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="modal-header">
          <div>
            <span style={{ fontSize: '0.72rem', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
              Task Details • ID: {task.task_id || 'Transient'}
            </span>
            <h2 style={{ fontSize: '1.25rem', fontWeight: 700, marginTop: '4px', color: 'var(--text-primary)' }}>
              {taskName}
            </h2>
          </div>
          <button className="modal-close-btn" onClick={onClose}>
            &times;
          </button>
        </div>

        {/* Body */}
        <div className="modal-body">
          {/* Metadata Grid */}
          <div className="info-grid">
            <div className="info-item">
              <label>Current Status</label>
              <span>{currStatus}</span>
            </div>
            <div className="info-item">
              <label>Previous Status</label>
              <span>{prevStatus}</span>
            </div>
            <div className="info-item">
              <label>Assignee</label>
              <span>{assignee}</span>
            </div>
            <div className="info-item">
              <label>Deadline</label>
              <span style={{ fontFamily: 'var(--font-mono)' }}>{deadline}</span>
            </div>
            <div className="info-item">
              <label>Jira Integration</label>
              <span>
                {jiraKey ? (
                  task.jira_url ? (
                    <a
                      href={task.jira_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="jira-badge"
                    >
                      {jiraKey} ↗
                    </a>
                  ) : (
                    <span className="jira-badge">{jiraKey}</span>
                  )
                ) : (
                  'Not connected'
                )}
              </span>
            </div>
            <div className="info-item">
              <label>Classification</label>
              <span>{task.classification || task.category || 'ACTIVE'}</span>
            </div>
          </div>

          {/* Completion Evidence */}
          <div style={{ marginBottom: '24px' }}>
            <label style={{ fontSize: '0.75rem', fontWeight: 700, textTransform: 'uppercase', color: 'var(--text-secondary)', display: 'block', marginBottom: '6px' }}>
              Completion Evidence & Change Justification
            </label>
            <div className="meeting-summary-box">
              {evidence}
            </div>
          </div>

          {/* Task History Timeline */}
          <div>
            <h4 style={{ fontSize: '0.9rem', fontWeight: 700, textTransform: 'uppercase', color: 'var(--text-secondary)', marginBottom: '8px' }}>
              Historical Transitions ({history.length})
            </h4>

            {loadingHistory ? (
              <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Loading transition history...</p>
            ) : history.length === 0 ? (
              <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                No prior transitions recorded. Task is in its initial meeting revision.
              </p>
            ) : (
              <div className="timeline">
                {history.map((h, i) => (
                  <div key={h.history_id || i} className="timeline-item">
                    <div className="timeline-marker" />
                    <div className="timeline-date">
                      {formatDate(h.created_at)} • Meeting ID: {h.meeting_id}
                    </div>
                    <div className="timeline-title">
                      {h.previous_status ? `${h.previous_status} → ${h.new_status}` : `Initial Status: ${h.new_status}`}
                      {h.new_assignee && h.previous_assignee && h.new_assignee !== h.previous_assignee && (
                        <span style={{ fontWeight: 400, marginLeft: 6 }}>
                          (Reassigned: {h.previous_assignee} → {h.new_assignee})
                        </span>
                      )}
                    </div>
                    {h.change_reason && (
                      <div className="timeline-desc">
                        "{h.change_reason}"
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

