function handler(event) {
  var request = event.request;
  if (request.uri === "/api/invocations") {
    request.uri = "/runtimes/__RUNTIME_ARN_ESCAPED__/invocations";
    request.querystring["qualifier"] = { value: "DEFAULT" };
  }
  return request;
}
