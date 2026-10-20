// Adapted from https://stackoverflow.com/a/58437909 (CC BY-SA 4.0, Scotty Jamison)
// Real example you can copy-paste and play with.
// jsonplaceholder.typicode.com provides a dummy rest-api
// for this sort of purpose.
async function doAsyncTask() {
  const url = (
    'https://jsonplaceholder.typicode.com/comments?' +
    new URLSearchParams({ postId: 1 }).toString()
  );

  const result = await fetch(url)
    .then(response => response.json());

  console.log('Fetched from: ' + url);
  console.log(result);
}

doAsyncTask();
