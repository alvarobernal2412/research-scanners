// Real example you can copy-paste and play with.
// jsonplaceholder.typicode.com provides a dummy rest-api
// for this sort of purpose.
async function runFetchJob() {
  const endpoint = (
    'https://jsonplaceholder.typicode.com/comments?' +
    new URLSearchParams({ postId: 1 }).toString()
  );

  const payload = await fetch(endpoint)
    .then(res => res.json());

  console.log('Fetched from: ' + endpoint);
  console.log(payload);
}

runFetchJob();
