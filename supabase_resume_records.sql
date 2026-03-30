create extension if not exists pgcrypto;

create table if not exists public.candidate_resumes (
    id uuid primary key default gen_random_uuid(),
    jr_number text not null default '',
    date_text text not null default '',
    skill text not null default '',
    file_name text not null default '',
    first_name text not null default '',
    last_name text not null default '',
    candidate_name text not null default '',
    email text not null default '',
    phone text not null default '',
    current_company text not null default '',
    total_experience text not null default '',
    relevant_experience text not null default '',
    current_ctc text not null default '',
    expected_ctc text not null default '',
    notice_period text not null default '',
    current_location text not null default '',
    preferred_location text not null default '',
    upload_to_sap text not null default '',
    actual_status text not null default 'Not Called',
    call_iteration text not null default 'First Call',
    comments_availability text not null default '',
    error_message text not null default '',
    resume_link text not null default '',
    created_by text not null default '',
    created_at timestamptz not null default timezone('utc', now()),
    modified_by text not null default '',
    modified_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_candidate_resumes_file_name on public.candidate_resumes (file_name);
create index if not exists idx_candidate_resumes_jr_number on public.candidate_resumes (jr_number);
