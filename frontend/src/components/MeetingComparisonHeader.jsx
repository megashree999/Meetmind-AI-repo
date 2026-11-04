import React from 'react';

export default function MeetingComparisonHeader({ currentMeeting, previousMeeting }) {
  if (!currentMeeting && !previousMeeting) {
    return null;
  }

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
    <div className="comparison-section">
      {/* Previous Meeting Card */}
      <div className="meeting-card previous">
        <span className="meeting-tag previous">Previous Meeting</span>
        {previousMeeting ? (
          <>
            <h3 className="meeting-title">
              {previousMeeting.meeting_title || `Meeting ${previousMeeting.meeting_id}`}
            </h3>
            <div className="meeting-meta">
              <span>Seq #{previousMeeting.sequence_number || 1}</span>
              <span>•</span>
              <span>ID: {previousMeeting.meeting_id}</span>
              <span>•</span>
              <span>{formatDate(previousMeeting.created_at)}</span>
            </div>
            <div className="meeting-summary-box">
              {previousMeeting.summary || 'No summary available for this meeting.'}
            </div>
          </>
        ) : (
          <div style={{ padding: '24px 0', color: 'var(--text-muted)', textAlign: 'center' }}>
            <p>No prior meeting found for baseline comparison.</p>
            <p style={{ fontSize: '0.8rem', marginTop: '4px' }}>
              Tasks in the current meeting will establish the project baseline.
            </p>
          </div>
        )}
      </div>

      {/* Current Meeting Card */}
      <div className="meeting-card current">
        <span className="meeting-tag current">Current Meeting</span>
        {currentMeeting ? (
          <>
            <h3 className="meeting-title">
              {currentMeeting.meeting_title || `Meeting ${currentMeeting.meeting_id}`}
            </h3>
            <div className="meeting-meta">
              <span>Seq #{currentMeeting.sequence_number || (previousMeeting ? (previousMeeting.sequence_number || 1) + 1 : 1)}</span>
              <span>•</span>
              <span>ID: {currentMeeting.meeting_id}</span>
              <span>•</span>
              <span>{formatDate(currentMeeting.created_at)}</span>
            </div>
            <div className="meeting-summary-box">
              {currentMeeting.summary || 'Summary generated from transcript analysis.'}
            </div>
          </>
        ) : (
          <div style={{ padding: '24px 0', color: 'var(--text-muted)', textAlign: 'center' }}>
            <p>No current meeting selected.</p>
          </div>
        )}
      </div>
    </div>
  );
}

