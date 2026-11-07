# Traceability: SGR-2

| AC | Criterion | Planned cases | Automated tests (test_refs) | In current test files | Result |
|---|---|---|---|---|---|
| AC-1 | verify_release raises TrustError 'links not covered by the s | 4 | test_an_extensionless_link_to_a_package_folder_fails, test_an_extensionless_link_to_a_file_fails, test_a_link_inside_a_real_subfolder_fails_but_stories_and_pycache_are_left_alone | 3/3 | GREEN |
| AC-2 | runtime_copy_check returns a problem naming the link, for th | 4 | test_a_link_the_signed_release_does_not_list_is_refused_whatever_its_name | 1/1 | GREEN |
| AC-3 | Links inside a real stories/ or __pycache__ folder are ignor | 4 | test_a_link_called_stories_or_pycache_is_not_left_alone_and_folder_links_are_not_followed | 1/1 | GREEN |
| AC-4 | Folder links are never followed: the check lists a folder li | 4 | test_a_link_called_stories_or_pycache_is_not_left_alone_and_folder_links_are_not_followed | 1/1 | GREEN |
