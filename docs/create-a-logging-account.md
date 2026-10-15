# Create a logging account

A service that ships its logs to OpenSearch, such as a collector, should log in
with an account that may only write to its one log index. This guide sets one up
in an environment with [`os-cli security`](cli.md#os-cli-security). The
permissions the account gets, and why each is needed, are in
`src/opensearch_client/security.py`.

## Before you start

You need:

- an administrator login to the cluster, in `OPENSEARCH_USER` and
  `OPENSEARCH_PASSWORD`;
- the cluster's REST port, usually 9200, in `OPENSEARCH_URL` or
  `OPENSEARCH_HOST`;
- the user name and the index name the service uses.

## Create the account

1. Write a new password to a file that only you can read. It must meet the
   cluster's password policy.

   ```
   umask 077
   printf '%s\n' "$(openssl rand -base64 24)Aa1!" > password.txt
   ```

1. Create the account:

   ```
   os-cli security create-account collector-logging --index collector-logging \
     --password-file password.txt
   ```

1. Check it. Every part of the account should be `true`; the index stays `false`
   until the service first writes to it.

   ```
   os-cli security check-account collector-logging --index collector-logging
   ```

1. Give the service the URL, the user name and the password. For the
   collectors, that is the Juju secret their logging setup reads. Then delete
   `password.txt`.

## Change the password

Delete the account and create it again with a new password file. In between,
the service cannot log in, so logs it sends then may be lost.

```
os-cli security delete-account collector-logging --apply
os-cli security create-account collector-logging --index collector-logging \
  --password-file password.txt
```

## Remove the account

```
os-cli security delete-account collector-logging          # shows what exists
os-cli security delete-account collector-logging --apply
```
