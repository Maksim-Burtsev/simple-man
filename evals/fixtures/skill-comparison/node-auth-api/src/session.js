function createSessionStore(clock = () => Date.now()) {
  const sessions = new Map();

  return {
    put(session) {
      sessions.set(session.token, session);
    },
    get(token) {
      return sessions.get(token) || null;
    },
    now() {
      return clock();
    },
  };
}

module.exports = { createSessionStore };
