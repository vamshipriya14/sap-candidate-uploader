# Candidate Uploader User Guide

## Purpose

This tool is used for two main tasks:

1. Upload candidate resumes and send them to SAP from the `Resume Pipeline`.
2. Send client emails from `Pending Client Emails`.

This guide is written for non-technical users.

---

## Before You Start

Make sure you have:

1. Your login access.
2. Candidate resumes ready in `PDF` or `DOCX` format.
3. Correct candidate details.
4. Correct JR Number.
5. Internet access.

---

## Main Pages

There are 2 important pages:

1. `Resume Pipeline`
2. `Client Emails`

---

## 1. Resume Pipeline

Use this page to:

1. Upload resumes
2. Review candidate details
3. Save candidate records
4. Upload candidates to SAP

### Step 1: Open Resume Pipeline

Open the app and click `Resume Pipeline` from the left menu.

![img_2.png](archive/images/img_2.png)

### Step 2: Upload Resumes

1. Click `Upload Resumes`
2. Select one or more resume files
3. Wait for parsing to complete

The system reads resume data and fills the table automatically.


![img_4.png](archive/images/img_4.png)

The system will show a progress bar while parsing resumes.
It will show `100%` when parsing is complete.

![img_5.png](archive/images/img_5.png)

### Step 3: Review the Main Table

Check every row carefully.

![img_7.png](archive/images/img_7.png)

Important fields:

1. `JR Number`
2. `First Name`
3. `Last Name`
4. `Email`
5. `Phone`
6. `Upload to SAP`

### Mandatory Rules

#### To save records in the main table

`JR Number` must not be empty.

If `JR Number` is empty:

1. The system will show an error
2. The save/sync will stop

#### Duplicate rule in main table

The system treats this combination as unique:

1. `JR Number`
2. `Email`
3. `Phone`

That means the same candidate should not be saved again with the same:

1. JR Number
2. Email
3. Phone

If you try to save a candidate with the same JR Number, Email, and Phone as an existing record:
1. The system will show an error 
2. The save/sync will stop
3. The existing record will not be updated

### Step 4: Correct Missing Data

If resume parsing misses some values, update them manually in the table.

You must especially check:

1. `JR Number`
2. `First Name`
3. `Last Name`
4. `Email`
5. `Phone`

### Step 5: Save the Table

When you edit the table, click `Save Table Changes` and the system saves records to the database.

If `JR Number` is missing, save will be blocked.

### Step 6: Upload to SAP

To send a candidate to SAP:

1. Set `Upload to SAP` = `Pending`
2. Confirm the candidate
3. Start the SAP upload

![img_8.png](archive/images/img_8.png)

### SAP Upload Mandatory Fields

For SAP upload, these fields are mandatory:

1. `JR Number`
2. `Email`
3. `Phone`
4. `First Name`
5. `Last Name`

If even one of these is missing:

1. The system will show an error
2. The SAP upload will not start

![img_9.png](archive/images/img_9.png)

### After SAP Upload

If upload is successful:

1. Candidate status is updated
2. Candidate becomes available in `Pending Client Emails`

---

## 2. Pending Client Emails

Use this page to:

1. Review candidates already uploaded to SAP
2. Edit email details
3. Edit candidate table details
4. Send the client email

### Step 1: Open Pending Client Emails

Click `Client Emails` in the left menu.

![img_10.png](archive/images/img_10.png)

### Step 2: Manage Email Signature

At the top of the page there is a section:

`Manage Your Email Signature`

It is collapsed by default.

Open it only if you want to:

1. Update your name
2. Update your job title
3. Update your phone number
4. Save your signature

![img_11.png](archive/images/img_11.png)

### Step 3: Select JR Number

Choose the JR from the dropdown.

Only candidates that:

1. Are uploaded to SAP
2. Are not yet emailed to client

will appear here.

![img_12.png](archive/images/img_12.png)

### Step 4: Review Email Details

The page shows:

1. `Client Recruiter Name`
2. `Email To`
3. `Email From`
4. `JR Number`
5. `CC`
6. `Subject`
7. `Email Body`

### Recruiter Name Selection Behavior

If you change `Client Recruiter Name`:

1. `Email To` updates automatically
2. The name in the greeting inside `Email Body` updates automatically

This happens only when you change the recruiter name from the prefilled value.

![img_13.png](archive/images/img_13.png)
![img_14.png](archive/images/img_14.png)
### Step 5: Edit Candidate Table

Below the email preview, the candidate table is editable.

You can update values such as:

1. `Candidate Name`
2. `Contact Number`
3. `Current Company`
4. `Total Experience`
5. `Relevant Experience`
6. `Current CTC`
7. `Expected CTC`
8. `Notice Period`
9. `Current Location`
10. `Preferred Location`
11. `comments/Availability`

Any changes made here are saved to the database.

![img_15.png](archive/images/img_15.png)

### Step 6: Send Email

Click `Send Email` only after checking everything.

### Email Send Mandatory Fields

For sending email, all draft fields are mandatory:

1. `JR Number`
2. `Client Recruiter Name`
3. `Email To`
4. `CC`
5. `Subject`
6. `Email Body`

Also, all fields in the candidate table must be filled.

If anything is missing:

1. The system will show an error
2. Email will not be sent


### After Email Is Sent

If email is sent successfully:

1. The email status is updated in the database
2. That JR/candidate set will no longer appear in `Pending Client Emails`

---

## Common Warnings and What They Mean

### `JR Number cannot be empty`

Meaning:

The candidate row is missing JR Number.

What to do:

Enter the correct JR Number in the main table.

### `SAP upload requires JR Number, Email, Phone, First Name, and Last Name`

Meaning:

One or more required SAP fields are missing.

What to do:

Fill all 5 fields and try again.

### `Cannot send email. Missing draft fields`

Meaning:

Some email fields are blank.

What to do:

Fill all email fields before sending.

### `Cannot send email. Candidate table has missing values`

Meaning:

Some fields in the candidate table are empty.

What to do:

Update the missing values in the editable candidate table and try again.

---

## Best Practices

1. Always verify `JR Number` first.
2. Always verify `Email` and `Phone`.
3. Do not upload to SAP with missing mandatory fields.
4. Review the editable candidate table before sending email.
5. Check the recruiter name before sending.
6. Update your signature only when necessary.

---

## Recommended Daily Process

1. Open `Resume Pipeline`
2. Upload resumes
3. Check parsed data
4. Fill missing mandatory fields
5. Save table
6. Set `Upload to SAP` = `Pending`
7. Upload to SAP
8. Open `Pending Client Emails`
9. Select JR
10. Verify recruiter and email details
11. Review/edit candidate table
12. Send client email

---

## Quick Checklist

### Before SAP Upload

1. JR Number filled
2. First Name filled
3. Last Name filled
4. Email filled
5. Phone filled

### Before Sending Client Email

1. Recruiter name correct
2. Email To correct
3. Subject correct
4. Email body correct
5. Candidate table fully filled
6. Signature correct

---


