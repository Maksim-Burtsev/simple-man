function authenticate(store, req) {
  const token = req.headers.authorization?.replace("Bearer ", "");
  if (!token) return { status: 401, body: "missing token" };

  const session = store.get(token);
  if (!session) return { status: 401, body: "invalid session" };

  return {
    status: 200,
    body: `hello ${session.userId}`,
  };
}

module.exports = { authenticate };
