# Create a logging account

A service that ships its logs to OpenSearch, such as a collector using
`OpensearchLogHandler`, should log in with an account that may write to its one
log index and do nothing else. This guide sets up such an account in an
environment with `os-cli security`.

## What the account is

The account is three Security plugin objects, all named after the user:

- the role `<user>-writer`;
- the internal user `<user>`, with a password;
- the role mapping that gives the user the role.

The role holds these permissions, and no others:

| Permission                | Where              | Why                                                            |
| ------------------------- | ------------------ | -------------------------------------------------------------- |
| `indices:data/write/bulk` | cluster            | Bulk requests are checked at the cluster level as well         |
| `indices:admin/get`       | the one log index  | The handler checks that the index exists before it writes      |
| `create_index`            | the one log index  | The handler creates the index when it is missing or deleted    |
| `index`                   | the one log index  | Writing documents, and adding the mapping of new fields        |

The index permissions name the index exactly, with no wildcard. The account
cannot search or read any index, write to another index, or delete anything.
The functional test `test_a_log_writer_account_may_create_and_write_its_index_and_nothing_else`
checks this against a real cluster.

`create_index` and `index` are default action groups; see
[default action groups](https://docs.opensearch.org/latest/security/access-control/default-action-groups/)
for the actions each one includes.

## Before you start

You need:

- an administrator login to the cluster, in `OPENSEARCH_USER` and
  `OPENSEARCH_PASSWORD`;
- the cluster's REST port in `OPENSEARCH_URL` (or `OPENSEARCH_HOST`), usually
  9200. The Security API is not reached through a Dashboards proxy;
- the user name and the index name the service uses.

## Create the account

1. Write a new password to a file that only you can read. The cluster's password
   policy applies, so a password it rejects makes the command fail before the
   user is created.

   ```
   umask 077
   printf '%s\n' "$(openssl rand -base64 24)Aa1!" > password.txt
   ```

1. Create the account:

   ```
   os-cli security create-account collector-logging --index collector-logging \
     --password-file password.txt
   ```

   If any part already exists, the command fails without changing it and says
   which parts it created before the failure.

1. Check it. Every part should be `true`. The index is `false` until the
   service first writes to it:

   ```
   os-cli security check-account collector-logging --index collector-logging
   ```

1. Give the service the URL, the user name and the password. For the
   collectors, that is the Juju secret their logging setup reads. Then delete
   `password.txt`.

## Change the password

Creation never overwrites, so changing a password means deleting the account
and creating it again. In between, the service cannot log in, so logs it sends
then may be lost.

```
os-cli security delete-account collector-logging --apply
os-cli security create-account collector-logging --index collector-logging \
  --password-file password.txt
```

## Remove the account

```
os-cli security delete-account collector-logging            # shows what exists
os-cli security delete-account collector-logging --apply
```

The mapping goes first, so the user loses access before anything else is
removed. The index and its documents are kept.
